"""独立的中文向量召回评估：临时库、固定语料、配置/数据指纹，不写用户记忆。"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import time

from .models import MemoryItem, MemoryType, Scope
from .retrieval.embedding import Embedder, EmbeddingError
from .retrieval.vector import VectorIndex
from .storage.database import Database

DEFAULT_DATASET = Path(__file__).parent / "data" / "embedding-zh-v1.json"


def load_embedding_dataset(path: str | Path = DEFAULT_DATASET) -> dict:
    raw = Path(path).read_bytes()
    if len(raw) > 2_000_000:
        raise ValueError("评估数据集超过 2 MiB")
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != {"dataset_id", "description", "documents", "queries"}:
        raise ValueError("评估数据集字段不正确")
    if any(not isinstance(data[key], str) or not data[key].strip() for key in ("dataset_id", "description")):
        raise ValueError("数据集必须有标识与说明")
    documents, queries = data["documents"], data["queries"]
    if not isinstance(documents, list) or not 1 <= len(documents) <= 10000:
        raise ValueError("documents 必须为 1..10000 条")
    if not isinstance(queries, list) or not 1 <= len(queries) <= 10000:
        raise ValueError("queries 必须为 1..10000 条")
    identifiers = set()
    for doc in documents:
        if not isinstance(doc, dict) or set(doc) != {"id", "text"}:
            raise ValueError("文档字段不正确")
        if not isinstance(doc["id"], str) or not 1 <= len(doc["id"]) <= 128 or doc["id"] in identifiers:
            raise ValueError("文档 ID 为空、重复或过长")
        if not isinstance(doc["text"], str) or not doc["text"].strip() or len(doc["text"]) > 350:
            raise ValueError("文档必须为 1..350 字符，防止被生产 render 上限隐式截断")
        identifiers.add(doc["id"])
    query_ids = set()
    for query in queries:
        if not isinstance(query, dict) or set(query) != {"id", "text", "relevant_ids", "category"}:
            raise ValueError("查询字段不正确")
        if (not isinstance(query["id"], str) or not 1 <= len(query["id"]) <= 128
                or query["id"] in query_ids):
            raise ValueError("查询 ID 为空、重复或过长")
        if not isinstance(query["text"], str) or not query["text"].strip() or len(query["text"]) > 4000:
            raise ValueError("查询正文无效")
        if query["category"] not in ("literal", "paraphrase", "negative"):
            raise ValueError("查询类别无效")
        relevant = query["relevant_ids"]
        if (not isinstance(relevant, list) or any(not isinstance(key, str) or key not in identifiers for key in relevant)
                or len(set(relevant)) != len(relevant)):
            raise ValueError("查询引用了未知或重复文档")
        if (query["category"] == "negative") != (len(relevant) == 0):
            raise ValueError("仅 negative 查询应当没有相关文档")
        query_ids.add(query["id"])
    return data


def run_embedding_evaluation(embedder: Embedder, *, dataset_path: str | Path = DEFAULT_DATASET,
                             min_score: float = 0.10, k: int = 3) -> dict:
    """只评估向量候选，不把分数解释为事实概率或完整聊天回答质量。"""
    if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= 100:
        raise ValueError("k 必须为 1..100 的整数")
    if isinstance(min_score, bool) or not isinstance(min_score, (int, float)) or not math.isfinite(min_score) or not -1 <= min_score <= 1:
        raise ValueError("min_score 必须在 [-1,1] 内")
    dataset = load_embedding_dataset(dataset_path)
    fingerprint = hashlib.sha256(json.dumps(dataset, ensure_ascii=False, sort_keys=True,
                                            separators=(",", ":")).encode("utf-8")).hexdigest()
    db = Database.open(":memory:")
    started = time.perf_counter()
    try:
        index = VectorIndex(db, embedder)
        scope = Scope("embedding-evaluation", "synthetic")
        now = datetime(2026, 10, 27, tzinfo=timezone.utc)
        with db.transaction():
            for doc in dataset["documents"]:
                db.repositories.memories.insert(MemoryItem(
                    memory_id=doc["id"], scope=scope, memory_type=MemoryType.EPISODIC,
                    body={"what_happened": doc["text"]}, recorded_at=now,
                ))
        report = {"dataset_id": dataset["dataset_id"], "dataset_sha256": fingerprint,
                  "embedding_namespace": index.model_name, "k": k, "min_score": min_score,
                  "document_count": len(dataset["documents"]), "query_count": len(dataset["queries"]),
                  "status": "ok", "quality_complete": True, "results": [],
                  "scope": "vector_candidates_only", "dataset_review": (
                      "synthetic_not_independently_reviewed" if Path(dataset_path).resolve() == DEFAULT_DATASET.resolve()
                      else "unverified_external_labels")}
        try:
            index.rebuild(scope, now)
            for query in dataset["queries"]:
                tick = time.perf_counter()
                result = index.search_with_status(scope, query["text"], limit=k, min_score=min_score)
                if result.degraded_reasons:
                    raise EmbeddingError(result.degraded_reasons[0])
                ids = [hit.memory_id for hit in result.hits]
                relevant = set(query["relevant_ids"])
                first = next((rank for rank, key in enumerate(ids, 1) if key in relevant), None)
                report["results"].append({
                    "id": query["id"], "category": query["category"], "retrieved_ids": ids,
                    "recall_at_k": len(relevant.intersection(ids)) / len(relevant) if relevant else None,
                    "reciprocal_rank": 1 / first if first else 0.0,
                    "top1_correct": bool(ids and ids[0] in relevant) if relevant else None,
                    "false_match": bool(ids) if not relevant else None,
                    "elapsed_ms": round((time.perf_counter() - tick) * 1000, 3),
                })
        except EmbeddingError as exc:
            report.update(status="unavailable", quality_complete=False, error_code=exc.code)
        # 失败或中断不使用只完成的前几题报告质量，防止乐观偏差。
        if report["quality_complete"]:
            positive = [row for row in report["results"] if row["category"] != "negative"]
            negative = [row for row in report["results"] if row["category"] == "negative"]
            def average(rows, field):
                return round(sum(row[field] for row in rows) / len(rows), 6) if rows else None
            report["metrics"] = {"positive_count": len(positive), "negative_count": len(negative),
                                 "recall_at_k": average(positive, "recall_at_k"),
                                 "mrr_at_k": average(positive, "reciprocal_rank"),
                                 "top1_accuracy": average(positive, "top1_correct"),
                                 "negative_false_match_rate": average(negative, "false_match"),
                                 "by_category": {category: {
                                     "count": len(rows := [row for row in positive if row["category"] == category]),
                                     "recall_at_k": average(rows, "recall_at_k"),
                                     "mrr_at_k": average(rows, "reciprocal_rank"),
                                 } for category in ("literal", "paraphrase")}}
        report["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
        report["execution_complete"] = report["quality_complete"]
        report["quality_accepted"] = False  # 裸评估不替代独立校准、人工审查和发布门槛。
        return report
    finally:
        db.close()
