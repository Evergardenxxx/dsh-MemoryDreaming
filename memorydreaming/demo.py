"""可运行场景：从零开始，看记忆怎么形成、检索、纠正、遗忘。

这是一份「能跑起来的说明书」。跑完它，你会看到：

    ① 没有核心时启动，引擎如实说「恢复失败」而不是编一个身份
    ② 建立版本化的身份/关系核心
    ③ 记录对话（含幂等：同一轮重复提交不会写两条）
    ④ 异步整理把事件抽成四类记忆
    ⑤ 工作检查点带版本号，并发修改会被拦下
    ⑥ 换窗口后恢复身份与未完活动
    ⑦ 一句多意图的问题，四类记忆分到不同的候选预算
    ⑧ 三天之后，48 小时之外的旧事仍然能被精确回查
    ⑨ 用户改变主意：旧事实结束有效期，新事实生效
    ⑩ 用户要求忘掉：删除传播到索引，不会换个形式再冒出来

运行：python -m memorydreaming demo
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock, load_timezone
from .errors import VersionConflict
from .extractors import RuleBasedExtractor
from .models import (
    CoreKind,
    Correction,
    ForgetSelector,
    MemoryType,
    Scope,
    Turn,
)
from .retrieval.context import render_bootstrap_context, render_retrieval_context
from .runtime import MemoryRuntime


def _section(title: str) -> None:
    print()
    print("=" * 76)
    print(title)
    print("=" * 76)


def _cleanup(path: Path) -> None:
    """清掉上次运行留下的库文件，让每次 demo 都是同一个起点。

    SQLite 的 WAL 模式会生成 -wal / -shm 两个附属文件，
    只删主文件可能留下不一致的状态，所以三个一起删。
    """
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()


def run_demo(db_path: str | Path | None = None) -> Path:
    """跑完整场景，返回数据库文件路径。"""
    path = Path(db_path) if db_path else Path(".memorydreaming") / "demo.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    _cleanup(path)

    # 固定时钟：demo 里的「刚才」「三天后」都是真实的时钟推进，不是改数字
    clock = FixedClock(datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc))
    runtime = MemoryRuntime(clock=clock, db_path=str(path))
    tz = load_timezone(runtime.settings.timezone)

    scope = Scope(user_id="u_linou", character_id="char_xiaoman")
    extractor = RuleBasedExtractor()

    # ---------------------------------------------------------------- ①
    _section("① 新窗口启动：此时还没有身份核心")
    first = runtime.bootstrap(scope.user_id, scope.character_id)
    print(render_bootstrap_context(first, tz))
    print("\n→ 注意 recovery_warning：引擎没有用临时身份顶替，而是要求先建立核心。")

    # ---------------------------------------------------------------- ②
    _section("② 建立版本化的身份核心与关系档案")
    identity = runtime.propose_core_update(
        scope,
        CoreKind.IDENTITY,
        {"名字": "小满", "身份": "长期陪伴角色", "说话风格": "温和、简短、不说教"},
        change_reason="初次设定，经用户确认",
    )
    relationship = runtime.propose_core_update(
        scope,
        CoreKind.RELATIONSHIP,
        {"关系": "认识三个月的朋友", "称呼": "尚未确认"},
        change_reason="初次设定关系档案",
    )
    print(f"身份核心 v{identity.core_version}：{identity.payload}")
    print(f"关系档案 v{relationship.core_version}：{relationship.payload}")

    # ---------------------------------------------------------------- ③
    _section("③ 开始一段可以跨窗口继续的活动")
    activity = runtime.register_activity(
        scope,
        title="《海底邮局》的故事",
        goal="把第三章讲完",
        open_loops=["第三章还没讲完"],
    )
    print(f"活动 {activity.activity_id}：{activity.title}，检查点版本 {activity.checkpoint_version}")

    # ---------------------------------------------------------------- ④
    _section("④ 记录用户说的话：写入事件 + 排入整理任务（同一事务）")
    utterances = [
        "我叫林鸥，你以后叫我阿鸥就行",
        "我们约好周末去海边，我要带相机",
        "我喜欢睡前听你讲故事",
    ]
    event_ids: list[str] = []
    for index, text in enumerate(utterances):
        turn = Turn(
            speaker="user",
            content=text,
            occurred_at=clock.now(),
            conversation_id="conv-001",
            activity_id=activity.activity_id,
        )
        observed = runtime.observe(turn, scope, idempotency_key=f"conv-001#{index}")
        event_ids.append(observed.event.event_id)
        print(f"· 「{text}」→ 事件 {observed.event.event_id}，任务 {observed.job_id}")

    duplicate = runtime.observe(
        Turn(
            speaker="user",
            content=utterances[0],
            occurred_at=clock.now(),
            conversation_id="conv-001",
            activity_id=activity.activity_id,
        ),
        scope,
        idempotency_key="conv-001#0",
    )
    print(f"· 重复提交同一条（幂等键 conv-001#0）→ duplicate={duplicate.duplicate}，新任务={duplicate.job_id}")

    # ---------------------------------------------------------------- ⑤
    _section("⑤ 异步整理：把原始事件抽成类型记忆")
    report = runtime.run_consolidation(extractor)
    print(
        f"任务 {report.claimed} 个：新建 {report.created} 条，"
        f"幂等跳过 {report.duplicates} 条，失败 {report.failed} 个"
    )
    stored = runtime.db.repositories.memories.list_by_scope(scope)
    for item in stored:
        print(f"· [{item.memory_type.label_zh}] {item.summary(60)}（{item.epistemic_status.value}）")

    # 抽取完成后会自动排一条去重任务，这里把它执行掉
    follow_up = runtime.run_consolidation()
    print(f"· 后续去重任务：合并 {follow_up.merged} 条重复记忆，失败 {follow_up.failed} 个")

    print()
    print("→ 用户明确说了「以后叫我阿鸥」，这属于关系设定，不能由模型自己改：")
    updated = runtime.propose_core_update(
        scope,
        CoreKind.RELATIONSHIP,
        {"称呼": "阿鸥"},
        change_reason="用户明确要求「以后叫我阿鸥」",
        expected_version=relationship.core_version,
        evidence_ids=[event_ids[0]],
    )
    print(f"   关系档案 v{updated.core_version}：{updated.payload}")
    print(f"   变更依据：{updated.evidence_ids}")

    # ---------------------------------------------------------------- ⑥
    _section("⑥ 工作检查点：版本号对不上就拒绝写入")
    print(f"当前版本 {activity.checkpoint_version}")
    new_version = runtime.checkpoint(
        activity.activity_id,
        {
            "open_loops": ["第三章还没讲完", "周末去海边记得带相机"],
            "checkpoint": {"chapter": 3, "last_line": "她把信塞进了海里那只绿色的邮筒"},
        },
        expected_version=activity.checkpoint_version,
    )
    print(f"保存成功 → 新版本 {new_version}")

    try:
        runtime.checkpoint(
            activity.activity_id,
            {"goal": "把第四章讲完"},
            expected_version=activity.checkpoint_version,  # 故意用旧版本
        )
    except VersionConflict as exc:
        print(f"用旧版本再写一次 → 被拦下：{exc}")
        print("→ 这正是「两个窗口同时改同一段活动」时该有的行为：重新读取，而不是静默覆盖。")

    # ---------------------------------------------------------------- ⑦
    _section("⑦ 换一个窗口：恢复身份、关系与未完活动")
    reopened = runtime.bootstrap(scope.user_id, scope.character_id)
    print(render_bootstrap_context(reopened, tz))

    # ---------------------------------------------------------------- ⑧
    _section("⑧ 一句多意图的问题：四类记忆各分到不同预算")
    question = "上次答应的睡前故事，今天接着讲吧"
    result = runtime.retrieve(question, scope)
    print(f"问题：{question}")
    print("类别路由：")
    for memory_type, score in sorted(
        result.plan.type_scores.items(), key=lambda kv: kv[1], reverse=True
    ):
        print(
            f"  {memory_type.label_zh}记忆  相关度 {score:.2f}  "
            f"候选预算 {result.plan.per_type_budget[memory_type]}"
        )
    print(f"展开粒度：{result.plan.expansion_mode}")
    print()
    print(render_retrieval_context(result, tz))
    print(f"\n检索轨迹：{result.trace}")

    # ---------------------------------------------------------------- ⑨
    _section("⑨ 三天之后：48 小时之外的旧事仍能精确回查")
    clock.advance(days=3)
    result_old = runtime.retrieve("我们第一次说去海边是什么时候", scope)
    print(render_retrieval_context(result_old, tz))
    print("\n→ 48 小时只影响「默认先展开什么细节」，不构成检索旧事的边界。")

    # ---------------------------------------------------------------- ⑩
    _section("⑩ 用户改变主意：旧事实截止，新事实生效")
    # 精确挑出「喜欢」那条偏好，而不是随便拿第一条语义记忆：
    # 这次检索里还有「称呼」「希望被称呼为」等语义记录，纠正错了会很难解释。
    preference = next(
        (
            item.memory
            for item in result.items
            if item.memory.memory_type is MemoryType.SEMANTIC
            and item.memory.body.get("predicate") == "喜欢"
        ),
        None,
    )
    if preference is None:
        print("（示例数据里没有找到偏好记忆，跳过纠正演示）")
    else:
        print(f"原记录：{preference.summary(60)}")
        corrected = runtime.correct(
            preference.memory_id,
            Correction(
                kind="change",  # 当时对，现在变了
                statement="我现在更想聊聊天，不太想听故事了",
                corrected_body={
                    "subject": "用户",
                    "predicate": "睡前偏好",
                    "obj": "更想聊聊天，不太想听故事",
                    "scope_note": "由用户口头更正",
                },
            ),
        )
        print(f"纠正结果：{corrected.note}")
        print(f"新记录：{corrected.new_memory.summary(60) if corrected.new_memory else '无'}")

        after = runtime.retrieve("睡前想做什么", scope)
        print()
        print(render_retrieval_context(after, tz))
        print("\n→ 旧记录没有消失，但被标成「历史值」；模型据此不会拿旧偏好当现在。")

    # ---------------------------------------------------------------- ⑪
    _section("⑪ 用户要求忘掉：删除传播到索引")
    preference_ids = [
        item.memory_id
        for item in runtime.retrieve("睡前偏好", scope).items
        if item.memory.memory_type is MemoryType.SEMANTIC
    ]
    forgotten = runtime.forget(
        ForgetSelector(memory_ids=preference_ids, reason="用户要求不再保留睡前偏好的记录")
    )
    print(f"删除 {len(forgotten.deleted_ids)} 条：{forgotten.deleted_ids}")
    print(f"连带失效的派生记忆：{forgotten.invalidated_derived}")

    empty = runtime.retrieve("睡前偏好", scope)
    print(f"\n再查一次 → 命中 {len(empty.items)} 条")
    print(f"缺口提示：{empty.gaps}")

    runtime.close()

    print()
    print("=" * 76)
    print(f"演示结束。数据库文件：{path.resolve()}")
    print("用 sqlite3 打开它，可以直接看到 events / memories / core_versions 等表。")
    print("=" * 76)
    return path
