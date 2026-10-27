"""可复现的本地规模检查；合成数据不代表真实模型质量。"""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import statistics
import tempfile
import time
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from memorydreaming import HostAdapter, MemoryItem, MemoryRuntime, MemoryType, Scope, SessionBinding
from memorydreaming.models import new_id


def benchmark(size=10000, queries=30, vectors=False):
    with tempfile.TemporaryDirectory(prefix="memorydreaming-benchmark-") as folder:
        with MemoryRuntime(db_path=str(Path(folder)/"memory.db")) as runtime:
            scope = Scope("benchmark", "character", "relationship")
            start = time.perf_counter()
            with runtime.db.transaction():
                for index in range(size):
                    runtime.db.repositories.memories.insert(MemoryItem(new_id("mem"), MemoryType.SEMANTIC, scope,
                        {"subject": "合成用户", "predicate": "记录", "obj": f"主题{index%100} 事件编号{index}"}))
            insert_seconds = time.perf_counter()-start
            start = time.perf_counter()
            vector_count = runtime.rebuild_vectors(scope) if vectors else 0
            index_seconds = time.perf_counter()-start
            adapter = HostAdapter(runtime)
            session = adapter.open_session(SessionBinding("benchmark", "character", "relationship", "bench"))
            timings = []
            for index in range(queries):
                start = time.perf_counter()
                session.prefetch(f"主题{index%100}")
                timings.append((time.perf_counter()-start)*1000)
            session.prefetch("重复查询")
            start = time.perf_counter()
            for _ in range(queries):
                session.prefetch("重复查询")
            cached_ms = (time.perf_counter()-start)*1000/queries
            result = {"size": size, "queries": queries, "insert_seconds": round(insert_seconds, 3),
                      "uncached_p50_ms": round(statistics.median(timings), 3),
                      "uncached_p95_ms": round(sorted(timings)[min(len(timings)-1, int(len(timings)*.95))], 3),
                      "cached_mean_ms": round(cached_ms, 3), "cache": session.cache_stats(),
                      "database_bytes": runtime.db.path.stat().st_size, "embedding": "hash" if vectors else "hash (unbuilt vectors)",
                      "indexed_vectors": vector_count, "index_seconds": round(index_seconds, 3),
                      "quality_complete": False}
            session.close(); adapter.close()
            return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, default=10000)
    parser.add_argument("--queries", type=int, default=30)
    parser.add_argument("--output")
    parser.add_argument("--vectors", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.size <= 100000 or not 1 <= args.queries <= 200:
        parser.error("size 1..100000; queries 1..200")
    result = benchmark(args.size, args.queries, args.vectors)
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(encoded+"\n", encoding="utf-8")
    print(encoded)
