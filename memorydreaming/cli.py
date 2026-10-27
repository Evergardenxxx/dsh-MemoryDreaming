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
import sys
from dataclasses import replace
from contextlib import redirect_stdout

from .clock import load_timezone, utcnow
from .config import Settings
from .demo import run_demo
from .evaluation import run_evaluation
from .extractors import RuleBasedExtractor
from .llm_extractor import DeepSeekExtractor
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
    extraction_eval = sub.add_parser("eval-extraction", help="独立中文抽取评估，不写记忆库")
    extraction_eval.add_argument("--extractor", choices=("rules", "deepseek", "replay"), default="rules")
    extraction_eval.add_argument("--dataset", help="自定义标注 JSON；默认内置中文合成回归集")
    extraction_eval.add_argument("--predictions", help="replay 模式读取保存的候选文件")
    extraction_eval.add_argument("--save-predictions", help="保存候选供离线复核；可能包含评估正文")
    extraction_eval.add_argument("--output", help="将不含正文的评分报告保存为 JSON")
    sub.add_parser("jobs", help="查看任务就绪、退避、租约与失败统计")

    reindex = sub.add_parser("reindex", help="重建某个角色的向量和实体索引")
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

    mcp = sub.add_parser("mcp", help="启动只读 MCP stdio 服务；身份由启动配置绑定")
    mcp.add_argument("--user", required=True)
    mcp.add_argument("--character", required=True)
    mcp.add_argument("--relationship", required=True)
    mcp.add_argument("--domain", default="real")
    mcp.add_argument("--allow-activity", action="append", default=[], help="显式允许的活动 ID，可重复")
    mcp.add_argument("--activity", help="默认焦点，必须同时在允许目录内")
    mcp.add_argument("--compact", action="store_true",
                     help="使用低上下文占用的 MCP 工具目录；服务端仍执行完整参数校验")

    bridge = sub.add_parser("bridge", help="启动原生宿主插件使用的 JSONL 生命周期桥接")
    bridge.add_argument("--user", required=True)
    bridge.add_argument("--character", required=True)
    bridge.add_argument("--relationship", required=True)
    bridge.add_argument("--domain", default="real")
    bridge.add_argument("--max-tokens", type=int, default=1200,
                        help="每轮记忆消息预算，默认 1200")
    bridge.add_argument("--auto-consolidation", action="store_true",
                        help="启动后台记忆整理器；事件入队后自动抽取、去重和摘要")
    bridge.add_argument("--extractor", choices=("rules", "deepseek"), default="rules",
                        help="后台抽取器；deepseek 从 DEEPSEEK_API_KEY 读取凭据")
    return parser


def _open_runtime(args: argparse.Namespace) -> MemoryRuntime:
    settings = Settings.from_env()
    if args.db:
        settings = settings.with_db(args.db)
    extractor = None
    if getattr(args, "command", None) == "bridge" and (
        getattr(args, "auto_consolidation", False) or getattr(args, "extractor", "rules") == "deepseek"
    ):
        settings = replace(settings, auto_consolidation=True)
        extractor = DeepSeekExtractor.from_env() if args.extractor == "deepseek" else RuleBasedExtractor()
    return MemoryRuntime(settings, consolidation_extractor=extractor)


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

    if args.command == "eval-extraction":
        from pathlib import Path
        from .extraction_evaluation import (DEFAULT_DATASET, ReplayExtractor, dataset_fingerprint,
                                            load_dataset, run_extraction_evaluation)
        dataset_path = args.dataset or DEFAULT_DATASET
        dataset = load_dataset(dataset_path)
        if args.extractor == "replay":
            if not args.predictions:
                parser.error("replay 模式需要 --predictions")
            extractor = ReplayExtractor(args.predictions, dataset["dataset_id"],
                                        dataset_sha256=dataset_fingerprint(dataset))
        else:
            if args.predictions:
                parser.error("--predictions 只用于 replay 模式")
            extractor = DeepSeekExtractor.from_env() if args.extractor == "deepseek" else RuleBasedExtractor()
        report = run_extraction_evaluation(extractor, dataset_path=dataset_path,
                                           predictions_output=args.save_predictions)
        rendered = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            Path(args.output).write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return 0 if report["passed"] == report["case_count"] else 1

    if args.command == "mcp":
        from .host import HostAdapter, SessionBinding
        from .mcp import McpServer
        for stream in (sys.stdin, sys.stdout):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
        # 配置降级和初始化诊断只进入 stderr，stdout 留给 MCP JSON-RPC。
        with redirect_stdout(sys.stderr):
            runtime = _open_runtime(args)
        try:
            binding = SessionBinding(args.user, args.character, args.relationship, "mcp-" + uuid.uuid4().hex,
                                     domain=args.domain, allowed_activity_ids=tuple(args.allow_activity),
                                     selected_activity_id=args.activity)
            session = HostAdapter(runtime).open_session(binding)
            return McpServer(session, compact=args.compact).serve()
        finally:
            runtime.close()

    if args.command == "bridge":
        from .bridge import BridgeServer
        for stream in (sys.stdin, sys.stdout):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
        # 桥接协议的 stdout 只允许输出一行一个响应；运行时诊断写入 stderr。
        with redirect_stdout(sys.stderr):
            runtime = _open_runtime(args)
        try:
            server = BridgeServer(runtime, user_id=args.user, character_id=args.character,
                                  relationship_id=args.relationship, domain=args.domain,
                                  max_tokens=args.max_tokens)
            return server.serve()
        finally:
            runtime.close()

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
        vector_count = runtime.rebuild_vectors(scope)
        entity_count = runtime.rebuild_entities(scope)
        print(f"已重建 {vector_count} 条向量（模型：{runtime.vectors.model_name}），{entity_count} 条实体提及")
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
