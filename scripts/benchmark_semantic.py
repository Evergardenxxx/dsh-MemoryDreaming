"""已下载真实权重的合成规模基线；不读用户数据库、不衡量标签质量。"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--revision", required=True)
    from memorydreaming.retrieval.profiles import EMBEDDING_PROFILES
    parser.add_argument("--profile", choices=EMBEDDING_PROFILES, default="generic")
    parser.add_argument("--dim", type=int, default=512)
    parser.add_argument("--query-prefix")
    parser.add_argument("--max-seq-length", type=int, default=512)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--size", type=int, default=10000)
    parser.add_argument("--queries", type=int, default=8)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if not 1 <= args.size <= 100000 or not 1 <= args.queries <= 200 or not 1 <= args.threads <= 64:
        parser.error("size 1..100000, queries 1..200, threads 1..64")
    import torch
    from memorydreaming import MemoryItem, MemoryRuntime, MemoryType, Scope
    from memorydreaming.models import new_id
    from memorydreaming.config import Settings
    from memorydreaming.retrieval.embedding import embedder_from_settings
    torch.set_num_threads(args.threads)
    prefix = args.query_prefix if args.query_prefix is not None else (
        "为这个句子生成表示以用于检索相关文章：" if args.profile == "generic" else "")
    settings = Settings(embedding_provider="sentence-transformers", embedding_profile=args.profile,
        embedding_model_path=args.model_path, embedding_revision=args.revision, embedding_dim=args.dim,
        embedding_query_prefix=prefix, embedding_max_seq_length=args.max_seq_length,
        embedding_batch_size=args.batch_size)
    # 本离线基线使用进程内编码，以使 torch_threads 的测量约定真正生效。
    # 正常宿主与质量验收使用带截止时间的独立模型进程。
    embedder = embedder_from_settings(settings)
    with tempfile.TemporaryDirectory(prefix="memorydreaming-real-model-scale-") as directory:
        with MemoryRuntime(db_path=str(Path(directory)/"scale.db"), embedder=embedder) as runtime:
            scope = Scope("scale-test", "synthetic")
            start = time.perf_counter()
            with runtime.db.transaction():
                for index in range(args.size):
                    runtime.db.repositories.memories.insert(MemoryItem(new_id("mem"), MemoryType.EPISODIC, scope,
                        {"what_happened": f"这是第{index}次在海边寻找编号{index%100}的贝壳，傍晚看日落。"},
                        recorded_at=datetime.now(timezone.utc)))
            write_seconds = time.perf_counter()-start
            start = time.perf_counter()
            count = runtime.rebuild_vectors(scope)
            index_seconds = time.perf_counter()-start
            timings = []
            for index in range(args.queries):
                start = time.perf_counter()
                runtime.vectors.search_with_status(scope, f"黄昏在沙滩上搜寻第{index}种小贝壳", limit=3, min_score=.5)
                timings.append((time.perf_counter()-start)*1000)
            report = {"revision": args.revision, "profile": args.profile,
                      "embedding_namespace": runtime.vectors.model_name,
                      "documents": args.size, "indexed": count,
                      "dimension": args.dim, "device": "cpu", "torch_threads": args.threads, "batch_size": args.batch_size,
                      "write_seconds": round(write_seconds, 3), "index_seconds": round(index_seconds, 3),
                      "query_count": len(timings), "query_p50_ms": round(statistics.median(timings), 3),
                      "query_max_ms": round(max(timings), 3), "quality_accepted": False,
                      "scope": "synthetic_scale_not_labelled_quality"}
    Path(args.output).write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
