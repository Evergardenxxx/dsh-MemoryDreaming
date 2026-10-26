"""检索评估：用一小批带标注的问题，量一量「找得准不准」。

为什么必须自带评估？

检索系统最容易产生错觉：随手试两个问题，感觉都对；换个说法就全错，
而你并不会发现。唯一能避免自欺的办法是：固定一组问题、固定期望答案、
每次改动后重跑同一套用例。

这里只测三件能被客观计分的事（架构文档第 8 节的指标口径）：

    证据召回率 Recall@K  期望的记忆有没有出现在前 K 条里
    MRR                 第一条命中的位置（1.0 表示永远排第一）
    无依据拒绝率        没有相关记忆时，是否返回空并给出缺口说明

**不测**「回答正确率」「虚假记忆率」：那两个必须有模型参与评分，
自己给自己打分没有意义。宁可留白，也不做假指标。

这套用例的规模很小（7 个），它的作用是回归保护，不是性能证明。
真实评估需要几百条来自实际对话的样本——那时候应该把 _seed 换成数据集加载器，
评分逻辑可以原样复用。
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import FixedClock
from .config import Settings
from .models import MemoryItem, MemoryType, Scope, new_id
from .runtime import MemoryRuntime

# 评估用的固定「现在」：2026-10-26 12:00（Asia/Shanghai）
NOW = datetime(2026, 10, 26, 4, 0, tzinfo=timezone.utc)

# 只看前 K 条。K 越大越容易「蒙对」，所以固定成 5 而不是全量。
TOP_K = 5


@dataclass
class EvalCase:
    """一个用例：问什么、期望命中哪条、还是期望「什么都别找到」。

    optional=True 的用例是「观察项」：它们需要真正的语义理解
    （例如用「失眠」去问一条写着「睡不着」的记忆），当前占位嵌入做不到。
    不计入分数，只是把差距摆在明面上——否则一个 100% 的漂亮数字毫无意义。
    """

    name: str
    query: str
    expected_ids: list[str] = field(default_factory=list)
    expect_empty: bool = False
    optional: bool = False


@dataclass
class CaseResult:
    case: EvalCase
    returned_ids: list[str]
    rank: int | None          # 期望记忆里第一条出现的位置，1 开始；没命中为 None
    passed: bool
    gap_count: int            # 返回了几条缺口说明


@dataclass
class EvalReport:
    results: list[CaseResult]
    recall: float
    mrr: float
    no_evidence_accuracy: float
    k: int = TOP_K

    def render(self) -> str:
        """把结果排成人能读的表格。"""
        scored = [result for result in self.results if not result.case.optional]
        optional = [result for result in self.results if result.case.optional]

        lines = [
            f"检索评估：{len(self.results)} 个用例"
            f"（计分 {len(scored)}，观察 {len(optional)}；Recall@{self.k}）",
            "",
            f"{'用例':<24}{'结果':<6}{'名次':<6}返回",
            "-" * 60,
        ]

        for result in scored:
            mark = "通过" if result.passed else "失败"
            rank = str(result.rank) if result.rank else "-"
            lines.append(f"{result.case.name:<24}{mark:<6}{rank:<6}{len(result.returned_ids)}")

        lines.append("-" * 60)
        lines.append(f"证据召回率 Recall@{self.k}：{self.recall:.1%}")
        lines.append(f"MRR：{self.mrr:.3f}")
        lines.append(f"无依据拒绝率：{self.no_evidence_accuracy:.1%}")

        if optional:
            lines.append("")
            lines.append("观察项（不计分，用于暴露当前模型的能力边界）：")
            for result in optional:
                mark = "命中" if result.passed else "未命中"
                lines.append(f"- {result.case.name}：{mark}（问法「{result.case.query}」）")

        failures = [result for result in scored if not result.passed]
        if failures:
            lines.append("")
            lines.append("失败明细：")
            for result in failures:
                lines.append(
                    f"- {result.case.name}：期望 {result.case.expected_ids or '无命中'}，"
                    f"实际 {result.returned_ids}"
                )

        return "\n".join(lines)


def run_evaluation(db_path: str | Path | None = None) -> EvalReport:
    """跑一遍评估。db_path 为空时用临时目录，跑完即弃。

    传入路径可以保留数据库，方便用 sqlite3 打开看「为什么这条没找到」。
    """
    temporary = tempfile.TemporaryDirectory()
    try:
        path = Path(db_path) if db_path else Path(temporary.name) / "eval.db"
        clock = FixedClock(NOW)
        runtime = MemoryRuntime(
            settings=Settings(db_path=path, timezone="Asia/Shanghai"),
            clock=clock,
        )
        try:
            scope = Scope(user_id="eval_user", character_id="eval_char")
            other_scope = Scope(user_id="eval_user", character_id="eval_other")

            ids = _seed(runtime, scope)
            _seed_unrelated(runtime, other_scope)

            cases = _build_cases(ids)
            empty_cases = [
                EvalCase(
                    name="无关问题不硬凑",
                    query="量子纠缠实验的结论是什么",
                    expect_empty=True,
                )
            ]
            runtime.rebuild_vectors(scope)
            runtime.rebuild_vectors(other_scope)

            results = [_run_case(runtime, scope, case) for case in cases]
            results += [_run_case(runtime, other_scope, case) for case in empty_cases]

            return _summarise(results)
        finally:
            runtime.close()
    finally:
        temporary.cleanup()


def _run_case(runtime: MemoryRuntime, scope: Scope, case: EvalCase) -> CaseResult:
    result = runtime.retrieve(case.query, scope, max_items=TOP_K)
    returned = result.memory_ids()

    rank: int | None = None
    for index, memory_id in enumerate(returned, start=1):
        if memory_id in case.expected_ids:
            rank = index
            break

    if case.expect_empty:
        passed = not returned
    else:
        passed = rank is not None

    return CaseResult(
        case=case,
        returned_ids=returned,
        rank=rank,
        passed=passed,
        gap_count=len(result.gaps),
    )


def _summarise(results: list[CaseResult]) -> EvalReport:
    # 观察项不参与打分：它们的失败是已知的模型能力问题，
    # 混进分母只会让指标变成一个说不清含义的数字。
    scored = [result for result in results if not result.case.optional]
    answerable = [result for result in scored if not result.case.expect_empty]
    unanswerable = [result for result in scored if result.case.expect_empty]

    hit = [result for result in answerable if result.rank is not None]
    recall = len(hit) / len(answerable) if answerable else 0.0
    mrr = sum(1.0 / result.rank for result in hit if result.rank) / len(answerable) if answerable else 0.0

    rejected = [result for result in unanswerable if result.passed]
    no_evidence = len(rejected) / len(unanswerable) if unanswerable else 1.0

    return EvalReport(
        results=results,
        recall=recall,
        mrr=mrr,
        no_evidence_accuracy=no_evidence,
    )


# ---------------------------------------------------------------------------
# 固定测试数据
# ---------------------------------------------------------------------------


def _seed(runtime: MemoryRuntime, scope: Scope) -> dict[str, str]:
    """写一批覆盖四类记忆的固定数据，返回 {逻辑名: memory_id}。

    刻意不用抽取器生成：评估要测的是「检索」，输入必须完全确定。
    抽取器质量由它自己的测试负责。
    """
    seeds = [
        # 逻辑名, 类型, 正文, 多少天前, 重要度
        ("name", MemoryType.SEMANTIC,
         {"subject": "用户", "predicate": "名字", "obj": "林鸥"}, 20, 0.9),
        ("pref_story", MemoryType.SEMANTIC,
         {"subject": "用户", "predicate": "喜欢", "obj": "睡前听故事"}, 20, 0.6),
        ("cat", MemoryType.SEMANTIC,
         {"subject": "用户", "predicate": "养了", "obj": "一只叫豆豆的橘猫"}, 25, 0.8),
        ("beach", MemoryType.EPISODIC,
         {"what_happened": "第一次去海边，捡了一整袋贝壳"}, 20, 0.7),
        ("ritual", MemoryType.PROCEDURAL,
         {"trigger": "用户说睡不着",
          "steps": ["讲一个短故事", "把灯调暗"],
          "expected_outcome": "慢慢睡着"}, 15, 0.6),
        ("trip", MemoryType.WORKING,
         {"goal": "周末去海边", "open_loops": ["带上相机", "查潮汐时间"]}, 3, 0.7),
        ("office_cat", MemoryType.EPISODIC,
         {"what_happened": "昨天在公司楼下遇到一只橘猫"}, 1, 0.5),
    ]

    ids: dict[str, str] = {}
    for key, memory_type, body, days_ago, importance in seeds:
        moment = NOW - timedelta(days=days_ago)
        item = MemoryItem(
            memory_id=new_id("mem"),
            memory_type=memory_type,
            scope=scope,
            body=body,
            importance=importance,
            occurred_start=moment,
            recorded_at=moment,
            source_event_ids=[f"evt_{key}"],
        )
        with runtime.db.transaction():
            runtime.db.repositories.memories.insert(item)
        ids[key] = item.memory_id
    return ids


def _seed_unrelated(runtime: MemoryRuntime, scope: Scope) -> None:
    """另一个角色名下的旧记忆。

    用它验证「没有相关证据」不是靠在空库里查询实现的——
    库里有东西、但和问题无关，才是真实场景。
    """
    moment = NOW - timedelta(days=30)
    item = MemoryItem(
        memory_id=new_id("mem"),
        memory_type=MemoryType.SEMANTIC,
        scope=scope,
        body={"subject": "用户", "predicate": "喜欢", "obj": "在阳台种薄荷"},
        occurred_start=moment,
        recorded_at=moment,
    )
    with runtime.db.transaction():
        runtime.db.repositories.memories.insert(item)


def _build_cases(ids: dict[str, str]) -> list[EvalCase]:
    """用例集：每个用例只盯一个检索能力，别混着测。

    期望值写成「任一命中即算通过」：一个问题往往有多种正确答法，
    要求「必须召回全部相关记忆」会让指标变成对实现细节的惩罚。
    """
    return [
        EvalCase(
            name="明确日期靠时间约束",
            query="昨天我们聊了什么",
            expected_ids=[ids["office_cat"]],
        ),
        EvalCase(
            name="旧事的精确回查",
            query="第一次去海边是什么时候",
            expected_ids=[ids["beach"]],
        ),
        EvalCase(
            name="程序记忆按触发条件召回",
            query="睡不着的时候你会怎么做",
            expected_ids=[ids["ritual"]],
        ),
        EvalCase(
            name="工作记忆的未完事项",
            query="周末去海边要带什么",
            expected_ids=[ids["trip"]],
        ),
        EvalCase(
            name="两个字的中文实体",
            query="橘猫",
            expected_ids=[ids["cat"], ids["office_cat"]],
        ),
        EvalCase(
            name="身份类事实",
            query="我的名字是什么",
            expected_ids=[ids["name"]],
        ),
        EvalCase(
            name="事件细节回查",
            query="海边那次捡了什么",
            expected_ids=[ids["beach"]],
        ),
        EvalCase(
            name="名字指代到实体",
            query="豆豆是谁",
            expected_ids=[ids["cat"]],
        ),
        # 下面是「观察项」：中文里同一个意思可以完全没有相同的字，
        # 只有真正的语义嵌入才能召回。留着它们，是为了让评估不撒谎。
        EvalCase(
            name="同义改写（失眠→睡不着）",
            query="失眠的时候你一般怎么哄我",
            expected_ids=[ids["ritual"]],
            optional=True,
        ),
        EvalCase(
            name="同义改写（养的那只猫→橘猫）",
            query="我养的那只猫叫什么",
            expected_ids=[ids["cat"]],
            optional=True,
        ),
    ]
