"""真实边界的回归：日期排他边界、硬约束、历史摘要和完整上下文预算。"""

from datetime import datetime, timedelta, timezone

from memorydreaming.models import LinkRelation, MemoryItem, MemoryStatus, MemoryType, Scope, new_id
from memorydreaming.retrieval.context import estimate_tokens, render_retrieval_context
from memorydreaming.retrieval.keyword import KeywordIndex
from memorydreaming.retrieval.timeexpr import parse_time_hint

from tests.helpers import NOW, RuntimeTestCase


class RetrievalRegressionTest(RuntimeTestCase):
    def test_fictional_memory_renders_its_domain(self) -> None:
        scope = Scope("u1", "c1", domain="shared_fiction")
        memory = MemoryItem(
            new_id("mem"), MemoryType.EPISODIC, scope,
            {"what_happened": "海边遇见独角兽"},
            occurred_start=NOW, recorded_at=NOW,
        )
        with self.runtime.db.transaction():
            self.repos.memories.insert(memory)

        result = self.runtime.retrieve("海边遇见独角兽", scope)

        self.assertIn(memory.memory_id, result.memory_ids())
        self.assertIn("虚构世界:shared_fiction", render_retrieval_context(result, self.tz))

    def test_calendar_ranges_end_at_next_period_start(self) -> None:
        now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
        cases = (
            ("上周", "2026-10-19", "2026-10-26"),
            ("本周", "2026-10-26", "2026-11-02"),
            ("上个月", "2026-09-01", "2026-10-01"),
            ("去年", "2025-01-01", "2026-01-01"),
            ("今年", "2026-01-01", "2027-01-01"),
        )
        for text, start, end in cases:
            with self.subTest(text=text):
                hint = parse_time_hint(text, now, self.tz)
                self.assertEqual(hint.start.astimezone(self.tz).isoformat(), start + "T00:00:00+08:00")
                self.assertEqual(hint.end.astimezone(self.tz).isoformat(), end + "T00:00:00+08:00")

    def test_continue_does_not_weaken_caller_time_range(self) -> None:
        old = self.add_memory(MemoryType.EPISODIC, {"what_happened": "海边约定"}, hours_ago=48)
        outside = self.add_memory(MemoryType.EPISODIC, {"what_happened": "海边约定"})
        result = self.runtime.retrieve(
            "继续查海边约定", self.scope,
            time_range=(NOW - timedelta(hours=60), NOW - timedelta(hours=36)),
        )
        self.assertTrue(result.plan.hard_time)
        self.assertIn(old.memory_id, result.memory_ids())
        self.assertNotIn(outside.memory_id, result.memory_ids())

    def test_continue_does_not_weaken_explicit_past_date(self) -> None:
        old = self.add_memory(MemoryType.EPISODIC, {"what_happened": "海边约定"}, hours_ago=24)
        outside = self.add_memory(MemoryType.EPISODIC, {"what_happened": "海边约定"})
        for text in ("继续查昨天的海边约定", "继续查2026年10月19日的海边约定"):
            with self.subTest(text=text):
                result = self.runtime.retrieve(text, self.scope)
                self.assertTrue(result.plan.hard_time)
                self.assertIn(old.memory_id, result.memory_ids())
                self.assertNotIn(outside.memory_id, result.memory_ids())

    def test_as_of_cannot_leak_future_records_through_summary(self) -> None:
        for text in ("海边约定带相机", "海边见面一起看日落", "海边一起收集贝壳"):
            self.add_memory(MemoryType.EPISODIC, {"what_happened": text})
        self.runtime.build_summaries(self.scope)
        self.assertTrue(self.repos.summaries.list_active(self.scope))

        result = self.runtime.retrieve("以前海边的事情", self.scope, as_of=NOW - timedelta(days=3))

        self.assertEqual(result.items, [])
        self.assertEqual(result.summaries, [])
        self.assertNotIn("相机", render_retrieval_context(result, self.tz))

    def test_tiny_budget_returns_empty_context_and_structured_gap(self) -> None:
        self.add_memory(MemoryType.EPISODIC, {"what_happened": "海边约定带相机"})

        result = self.runtime.retrieve("海边约定", self.scope, token_budget=1)

        self.assertEqual(result.items, [])
        self.assertEqual(result.trace.token_estimate, 0)
        self.assertEqual(render_retrieval_context(result, self.tz), "")
        self.assertTrue(any("预算" in gap for gap in result.gaps))

    def test_full_context_budget_includes_summary_and_fence(self) -> None:
        for text in ("海边约定带相机", "海边见面一起看日落", "海边一起收集贝壳"):
            self.add_memory(MemoryType.EPISODIC, {"what_happened": text})
        self.runtime.build_summaries(self.scope)
        for budget in (160, 240, 400):
            with self.subTest(budget=budget):
                result = self.runtime.retrieve("以前海边的事情", self.scope, token_budget=budget)
                context = render_retrieval_context(result, self.tz)
                self.assertTrue(context)
                self.assertLessEqual(estimate_tokens(context), budget)
                self.assertEqual(result.trace.token_estimate, estimate_tokens(context))
                self.assertEqual(result.trace.selected, len(result.items))

    def test_scope_filters_run_before_keyword_candidate_limit(self) -> None:
        cases = (
            ("domain", Scope("u1", "domain-case"), Scope("u1", "domain-case", domain="shared_fiction")),
            ("relationship", Scope("u1", "relation-case", relationship_id="r1"), Scope("u1", "relation-case", relationship_id="r2")),
            ("activity", Scope("u1", "activity-case", activity_id="a1"), Scope("u1", "activity-case", activity_id="a2")),
        )
        keyword = KeywordIndex(self.runtime.db.connection)
        for name, allowed_scope, foreign_scope in cases:
            with self.subTest(dimension=name):
                # 越权候选超过通道容量且先入库，不能先 LIMIT 再过滤。
                with self.runtime.db.transaction():
                    for index in range(50):
                        self.repos.memories.insert(MemoryItem(
                            f"foreign-{name}-{index:03}", MemoryType.EPISODIC, foreign_scope,
                            {"what_happened": "海边约定带相机"},
                            occurred_start=NOW, recorded_at=NOW,
                        ))
                    target = MemoryItem(
                        f"target-{name}", MemoryType.EPISODIC, allowed_scope,
                        {"what_happened": "海边约定带相机"},
                        occurred_start=NOW, recorded_at=NOW,
                    )
                    self.repos.memories.insert(target)

                for term in ("海边", "海边约定"):
                    with self.subTest(term=term):
                        hits = keyword.search(
                            [term], allowed_scope.user_id, allowed_scope.character_id,
                            limit=1, scope=allowed_scope,
                        )
                        self.assertEqual([hit.memory_id for hit in hits], [target.memory_id])
                result = self.runtime.retrieve("海边约定", allowed_scope)
                self.assertEqual(result.memory_ids(), [target.memory_id])

    def test_without_fts_long_terms_still_use_like(self) -> None:
        memory = self.add_memory(MemoryType.EPISODIC, {"what_happened": "海边遇见独角兽"})
        with self.runtime.db.transaction():
            self.runtime.db.connection.execute(
                "UPDATE schema_meta SET value = 'none' WHERE key = 'keyword_tokenizer'"
            )

        hits = KeywordIndex(self.runtime.db.connection).search(["独角兽"], "u1", "c1")

        self.assertEqual([hit.memory_id for hit in hits], [memory.memory_id])

    def test_reindex_preserves_other_domains_and_activities(self) -> None:
        scopes = (
            Scope("u1", "c1", activity_id="a1"),
            Scope("u1", "c1", activity_id="a2"),
            Scope("u1", "c1", domain="shared_fiction"),
        )
        memories = []
        with self.runtime.db.transaction():
            for scope in scopes:
                memory = MemoryItem(
                    new_id("mem"), MemoryType.EPISODIC, scope,
                    {"what_happened": "海边约定带相机"},
                    occurred_start=NOW, recorded_at=NOW,
                )
                self.repos.memories.insert(memory)
                self.runtime.vectors.index_memory(memory, NOW)
                memories.append(memory)

        self.runtime.rebuild_vectors(scopes[0])
        self.runtime.rebuild_vectors(self.scope)

        for scope, memory in zip(scopes, memories):
            with self.subTest(scope=scope):
                hits = self.runtime.vectors.search(scope, "海边约定带相机", min_score=0.1)
                self.assertIn(memory.memory_id, [hit.memory_id for hit in hits])
        self.assertEqual(self.runtime.vectors.count(), 3)

    def test_reindex_drops_retracted_and_merged_vectors(self) -> None:
        canonical = self.add_memory(MemoryType.EPISODIC, {"what_happened": "海边约定"})
        retracted = self.add_memory(MemoryType.EPISODIC, {"what_happened": "海边记错的事"})
        duplicate = self.add_memory(MemoryType.EPISODIC, {"what_happened": "海边约定的副本"})
        self.runtime.rebuild_vectors(self.scope)
        with self.runtime.db.transaction():
            self.repos.memories.update_status(retracted.memory_id, MemoryStatus.RETRACTED, NOW)
            self.repos.memories.update_status(duplicate.memory_id, MemoryStatus.SUPERSEDED, NOW)
            self.repos.links.add(duplicate.memory_id, canonical.memory_id, LinkRelation.PART_OF, NOW)

        count = self.runtime.rebuild_vectors(self.scope)

        self.assertEqual(count, 1)
        self.assertEqual(self.runtime.vectors.count(), 1)
