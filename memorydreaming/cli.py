"""命令行入口：不写代码也能看一眼引擎在做什么。

常用：
    python -m memorydreaming init                # 建库
    python -m memorydreaming demo                # 跑完整场景（推荐先看这个）
    python -m memorydreaming bootstrap --user u1 --character c1
    python -m memorydreaming observe   --user u1 --character c1 --content "我喜欢猫"
    python -m memorydreaming consolidate         # 处理待抽取任务
    python -m memorydreaming retrieve  --user u1 --character c1 "上次说的猫"
    python -m memorydreaming jobs                # 还有多少任务没处理

这些命令只是薄薄一层包装，真正的逻辑都在 runtime.py，
所以命令行能做的事，代码里一定也能做。
"""

from __future__ import annotations

import argparse
import json
import uuid

from .clock import load_timezone, utcnow
from .config import Settings
from .demo import run_demo
from .evaluation import run_evaluation
from .extractors import RuleBasedExtractor
from .models import ForgetSelector, Scope, Turn
from .retrieval.context import render_bootstrap_context, render_retrieval_context
from .runtime import MemoryRuntime


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memorydreaming",
        description="长期记忆引擎：身份、关系、共同经历可跨窗口恢复",
    )
    # 全局参数放在子命令前面也生效：memorydreaming --db x.db init
    parser.add_argument("--db", help="SQLite 文件路径，默认 .memorydreaming/memory.db")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化数据库结构")
    sub.add_parser("demo", help="跑一遍完整演示场景")
    sub.add_parser("eval", help="跑一遍检索评估（Recall@5 / MRR / 无依据拒绝）")
    sub.add_parser("jobs", help="查看任务就绪、退避、租约与失败统计")

    reindex = sub.add_parser("reindex", help="重建某个角色的向量索引")
    reindex.add_argument("--user", required=True)
    reindex.add_argument("--character", required=True)

    consolidate = sub.add_parser("consolidate", help="执行待处理的抽取任务（使用示例规则抽取器）")
    consolidate.add_argument("--limit", type=int, default=20, help="本次最多处理多少任务")

    bootstrap = sub.add_parser("bootstrap", help="恢复身份、关系与活动目录")
    bootstrap.add_argument("--user", required=True)
    bootstrap.add_argument("--character", required=True)
    bootstrap.add_argument("--activity", help="指定要继续的活动 ID")

    observe = sub.add_parser("observe", help="保存一轮对话")
    observe.add_argument("--user", required=True)
    observe.add_argument("--character", required=True)
    observe.add_argument("--content", required=True)
    observe.add_argument("--speaker", default="user", choices=["user", "character", "system"])
    observe.add_argument("--conversation", default="cli")
    observe.add_argument("--activity", help="归属的活动 ID")
    observe.add_argument("--key", help="幂等键；不填则自动生成，重复提交请手动指定同一个值")

    retrieve = sub.add_parser("retrieve", help="检索记忆")
    retrieve.add_argument("--user", required=True)
    retrieve.add_argument("--character", required=True)
    retrieve.add_argument("query", help="要检索的问题原文")

    forget = sub.add_parser("forget", help="按 ID 删除记忆")
    forget.add_argument("memory_id")
    forget.add_argument("--reason", default="用户要求删除")
    return parser


def _open_runtime(args: argparse.Namespace) -> MemoryRuntime:
    settings = Settings.from_env()
    if args.db:
        settings = settings.with_db(args.db)
    return MemoryRuntime(settings)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "demo":
        run_demo(args.db)
        return 0

    if args.command == "eval":
        # 评估用临时库，跑完即弃；传 --db 可以保留下来慢慢看
        print(run_evaluation(args.db).render())
        return 0

    runtime = _open_runtime(args)
    try:
        return _dispatch(args, runtime)
    finally:
        runtime.close()


def _dispatch(args: argparse.Namespace, runtime: MemoryRuntime) -> int:
    tz = load_timezone(runtime.settings.timezone)

    if args.command == "init":
        print(f"数据库已就绪：{runtime.settings.db_path.resolve()}")
        print(f"关键词索引：{runtime.db.meta_get('keyword_tokenizer')}")
        print(f"结构版本：{runtime.db.meta_get('schema_version')}")
        return 0

    if args.command == "jobs":
        print(json.dumps(runtime.job_stats(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "reindex":
        scope = Scope(user_id=args.user, character_id=args.character)
        count = runtime.rebuild_vectors(scope)
        print(f"已重建 {count} 条向量（模型：{runtime.vectors.model_name}）")
        return 0

    if args.command == "consolidate":
        report = runtime.run_consolidation(RuleBasedExtractor(), limit=args.limit)
        print(json.dumps(report.__dict__, ensure_ascii=False, indent=2))
        return 1 if report.failed or report.lease_lost else 0

    if args.command == "bootstrap":
        result = runtime.bootstrap(args.user, args.character, args.activity)
        print(render_bootstrap_context(result, tz))
        return 0

    if args.command == "forget":
        result = runtime.forget_fact(
            ForgetSelector(memory_ids=[args.memory_id], reason=args.reason)
        )
        print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
        return 0

    scope = Scope(user_id=args.user, character_id=args.character)

    if args.command == "observe":
        turn = Turn(
            speaker=args.speaker,
            content=args.content,
            occurred_at=utcnow(),
            conversation_id=args.conversation,
            activity_id=args.activity,
        )
        key = args.key or f"cli-{uuid.uuid4().hex}"
        result = runtime.observe(turn, scope, idempotency_key=key)
        status = "重复，未重复写入" if result.duplicate else "已保存"
        print(f"{status}：事件 {result.event.event_id}，任务 {result.job_id}，幂等键 {key}")
        return 0

    if args.command == "retrieve":
        result = runtime.retrieve(args.query, scope)
        print(render_retrieval_context(result, tz))
        print()
        print(f"检索轨迹：{result.trace}")
        return 0

    print(f"未知命令：{args.command}")
    return 2
