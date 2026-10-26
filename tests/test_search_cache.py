from __future__ import annotations

from unittest.mock import patch

from memorydreaming.host import HostAdapter, SessionBinding
from memorydreaming.models import ForgetSelector, MemoryType
from tests.helpers import RuntimeTestCase


class SearchCacheTests(RuntimeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.scope = self.scope.__class__("u1", "c1", "r1")
        self.item = self.add_memory(
            MemoryType.SEMANTIC,
            {"subject": "用户", "predicate": "喜欢", "obj": "海边故事"},
        )
        self.session = HostAdapter(
            self.runtime, clock=self.clock, retrieval_cache_ttl_seconds=60,
        ).open_session(SessionBinding("u1", "c1", "r1", "cache-window"))

    @staticmethod
    def args(query="喜欢海边故事"):
        return {
            "query": query, "activity_ref": None, "time_range": None,
            "detail": "evidence", "max_items": 20, "max_tokens": 4096,
        }

    def test_repeated_search_reuses_references_without_running_pipeline_again(self):
        with patch.object(self.runtime, "retrieve", wraps=self.runtime.retrieve) as retrieve:
            first = self.session.call_tool("memory_search", self.args())
            second = self.session.call_tool("memory_search", self.args())
        self.assertEqual(retrieve.call_count, 1)
        self.assertEqual(first["data"]["evidence"][0]["ref"], second["data"]["evidence"][0]["ref"])

    def test_content_revision_invalidates_cache_even_without_scope_epoch_bump(self):
        with patch.object(self.runtime, "retrieve", wraps=self.runtime.retrieve) as retrieve:
            self.session.call_tool("memory_search", self.args())
            self.repos.memories.update_content(
                self.item.memory_id,
                {"subject": "用户", "predicate": "喜欢", "obj": "海边新故事"},
                self.clock.now(),
            )
            result = self.session.call_tool("memory_search", self.args())
        self.assertEqual(retrieve.call_count, 2)
        self.assertIn("海边新故事", result["data"]["evidence"][0]["text"])

    def test_forgetting_bumps_epoch_and_never_returns_cached_evidence(self):
        with patch.object(self.runtime, "retrieve", wraps=self.runtime.retrieve) as retrieve:
            self.session.call_tool("memory_search", self.args())
            self.runtime.forget(ForgetSelector(memory_ids=[self.item.memory_id]))
            result = self.session.call_tool("memory_search", self.args())
        self.assertEqual(retrieve.call_count, 2)
        self.assertEqual(result["status"], "not_found")

