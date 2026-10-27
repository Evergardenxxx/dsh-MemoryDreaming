"""独立校准/验收、冻结阈值与审查门槛；模型执行完成不等于质量合格。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import unicodedata

from .embedding_evaluation import load_embedding_dataset, run_embedding_evaluation
from .extraction_evaluation import dataset_fingerprint, load_dataset, run_extraction_evaluation


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _normal(value):
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


@dataclass(frozen=True)
class QualityPolicy:
    """须在最终评估前确定；数量不足与模型不合格分别报告。"""

    min_cases: int = 100
    min_positive: int = 50
    min_negative: int = 50
    min_recall: float = .80
    min_precision: float = .95
    max_false_memory_rate: float = .02
    min_paraphrase_recall: float = .75

    def __post_init__(self):
        for name in ("min_cases", "min_positive", "min_negative"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("最小样本量必须为正整数")
        for name in ("min_recall", "min_precision", "max_false_memory_rate", "min_paraphrase_recall"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("质量门槛必须在 [0,1] 内")


def disjoint_datasets(calibration, evaluation, kind):
    """同一语料库可以复用，但标签查询/抽取原文不能跨校准和验收重叠。"""
    if calibration["dataset_id"] == evaluation["dataset_id"]:
        raise ValueError("校准和验收须使用不同 dataset_id")
    field, text = ("queries", "text") if kind == "embedding" else ("cases", "content")
    left, right = calibration[field], evaluation[field]
    if {row["id"] for row in left}.intersection(row["id"] for row in right):
        raise ValueError("校准和验收的样例 ID 不可重叠")
    if {_normal(row[text]) for row in left}.intersection(_normal(row[text]) for row in right):
        raise ValueError("校准和验收的样例正文不可重叠，包括空白/大小写改写")


def _model_identity(embedder):
    # 本地绝对路径不输出；namespace/版本配置足以区分权重与投影。
    return _digest({"name": embedder.name, "dim": embedder.dim,
                    "signature": getattr(embedder, "cache_signature", None)})


def calibrate_embedding(embedder, *, calibration_path, evaluation_path, thresholds=None,
                        k=3, policy=QualityPolicy()):
    calibration = load_embedding_dataset(calibration_path)
    evaluation = load_embedding_dataset(evaluation_path)
    disjoint_datasets(calibration, evaluation, "embedding")
    thresholds = list(thresholds if thresholds is not None else [i/100 for i in range(10, 91, 5)])
    if not thresholds or len(thresholds) > 100 or any(isinstance(v, bool) or not isinstance(v, (int, float))
            or not math.isfinite(v) or not -1 <= v <= 1 for v in thresholds):
        raise ValueError("校准阈值必须为最多 100 个 [-1,1] 有限数")
    candidates = []
    # 反复评分只对 calibration 调用，evaluation 标签完全不用于选阈值。
    for threshold in sorted(set(thresholds)):
        report = run_embedding_evaluation(embedder, dataset_path=calibration_path, min_score=threshold, k=k)
        if not report.get("quality_complete"):
            return {"status": "unavailable", "error_code": report.get("error_code", "EMBEDDING_UNAVAILABLE"),
                    "accepted": False}
        metrics = report["metrics"]
        safe = metrics["negative_count"] > 0 and metrics["negative_false_match_rate"] <= policy.max_false_memory_rate
        candidates.append({"threshold": threshold, "safe": safe, "metrics": metrics})
    safe = [row for row in candidates if row["safe"]]
    chosen = max(safe or candidates, key=lambda row: (
        -(row["metrics"]["negative_false_match_rate"] or 0) if not safe else 0,
        row["metrics"]["recall_at_k"] or 0, row["metrics"]["mrr_at_k"] or 0, row["threshold"],
    ))
    artifact = {"format": "memorydreaming.embedding-calibration.v1", "status": "calibrated",
                "calibration_dataset_sha256": dataset_fingerprint(calibration),
                "evaluation_dataset_sha256": dataset_fingerprint(evaluation),
                "model_identity": _model_identity(embedder), "k": k,
                "min_score": chosen["threshold"], "calibration_constraint_met": bool(safe),
                "policy": asdict(policy), "candidates": candidates}
    artifact["artifact_sha256"] = _digest(artifact)
    return artifact


def _attestation(review, calibration_digest, evaluation_digest):
    if not isinstance(review, dict):
        return False
    # 由可信外部评审者提供，程序只复核绑定与完整性，不伪造评审身份。
    if (review.get("independent_human_review") is not True
            or not isinstance(review.get("reviewer"), str) or not review["reviewer"].strip()
            or review.get("calibration_dataset_sha256") != calibration_digest
            or review.get("evaluation_dataset_sha256") != evaluation_digest):
        return False
    try:
        when = datetime.fromisoformat(review["reviewed_at"])
        return when.tzinfo is not None
    except (KeyError, TypeError, ValueError):
        return False


def _reasons(policy, *, count, positive, negative, recall, precision, false_rate,
             complete, independently_reviewed, real_model):
    reasons = []
    if not complete:
        reasons.append("INCOMPLETE_EXECUTION")
    if not independently_reviewed:
        reasons.append("INDEPENDENT_HUMAN_REVIEW_REQUIRED")
    if not real_model:
        reasons.append("REAL_MODEL_EVIDENCE_REQUIRED")
    if count < policy.min_cases or positive < policy.min_positive or negative < policy.min_negative:
        reasons.append("INSUFFICIENT_EVALUATION_SAMPLES")
    if recall is None or recall < policy.min_recall:
        reasons.append("RECALL_BELOW_POLICY")
    if precision is not None and precision < policy.min_precision:
        reasons.append("PRECISION_BELOW_POLICY")
    if false_rate is None or false_rate > policy.max_false_memory_rate:
        reasons.append("FALSE_MEMORY_RATE_ABOVE_POLICY")
    return reasons


def evaluate_embedding_acceptance(embedder, *, calibration_path, evaluation_path, calibration_artifact,
                                  review=None):
    calibration = load_embedding_dataset(calibration_path)
    evaluation = load_embedding_dataset(evaluation_path)
    disjoint_datasets(calibration, evaluation, "embedding")
    artifact = dict(calibration_artifact)
    expected = artifact.pop("artifact_sha256", None)
    if expected != _digest(artifact) or artifact.get("format") != "memorydreaming.embedding-calibration.v1":
        raise ValueError("校准文件摘要或格式无效")
    if (artifact["calibration_dataset_sha256"] != dataset_fingerprint(calibration)
            or artifact["evaluation_dataset_sha256"] != dataset_fingerprint(evaluation)
            or artifact["model_identity"] != _model_identity(embedder)):
        raise ValueError("冻结阈值与数据或模型配置不匹配，不能静默重新校准")
    policy = QualityPolicy(**artifact["policy"])
    report = run_embedding_evaluation(embedder, dataset_path=evaluation_path,
                                     min_score=artifact["min_score"], k=artifact["k"])
    metrics = report.get("metrics", {})
    from .retrieval.embedding import SentenceTransformerEmbedder
    real_model = isinstance(embedder, SentenceTransformerEmbedder)
    reviewed = _attestation(review, artifact["calibration_dataset_sha256"], artifact["evaluation_dataset_sha256"])
    reasons = _reasons(policy, count=report["query_count"], positive=metrics.get("positive_count", 0),
        negative=metrics.get("negative_count", 0), recall=metrics.get("recall_at_k"), precision=None,
        false_rate=metrics.get("negative_false_match_rate"), complete=report.get("quality_complete", False),
        independently_reviewed=reviewed, real_model=real_model)
    paraphrase = metrics.get("by_category", {}).get("paraphrase", {})
    if not paraphrase.get("count") or paraphrase.get("recall_at_k", 0) < policy.min_paraphrase_recall:
        reasons.append("PARAPHRASE_RECALL_BELOW_POLICY")
    if not artifact["calibration_constraint_met"]:
        reasons.append("CALIBRATION_CONSTRAINT_UNMET")
    report["acceptance"] = {"accepted": not reasons, "reasons": reasons, "policy": asdict(policy),
        "threshold_frozen": True, "calibration_artifact_sha256": expected, "real_model_executed": real_model,
        "independent_human_review": reviewed, "scope": "vector_candidates_only"}
    report["quality_accepted"] = not reasons
    if reviewed:
        report["dataset_review"] = "independent_human_review_attested"
    return report


def evaluate_extraction_acceptance(extractor, *, calibration_path, evaluation_path, policy=QualityPolicy(), review=None,
                                   predictions_output=None):
    calibration, evaluation = load_dataset(calibration_path), load_dataset(evaluation_path)
    disjoint_datasets(calibration, evaluation, "extraction")
    report = run_extraction_evaluation(extractor, dataset_path=evaluation_path, predictions_output=predictions_output)
    from .llm_extractor import DeepSeekExtractor
    from .llm_extractor import DeepSeekClient
    import urllib.request
    real = (isinstance(extractor, DeepSeekExtractor) and isinstance(extractor.client, DeepSeekClient)
            and extractor.client._opener is urllib.request.urlopen)
    reviewed = _attestation(review, dataset_fingerprint(calibration), dataset_fingerprint(evaluation))
    positives = sum(bool(case["labels"]) for case in evaluation["cases"])
    reasons = _reasons(policy, count=report["case_count"], positive=positives, negative=report["negative_cases"],
        recall=report["recall"], precision=report["precision"], false_rate=report["false_memory_case_rate"],
        complete=report["errors"] == 0, independently_reviewed=reviewed, real_model=real)
    if report["precision"] is None:
        reasons.append("PRECISION_UNDEFINED")
    report["acceptance"] = {"accepted": not reasons, "reasons": reasons, "policy": asdict(policy),
        "independent_human_review": reviewed, "real_model_executed": real,
        "scope": "extraction_candidates_only", "calibration_dataset_sha256": dataset_fingerprint(calibration)}
    report["quality_accepted"] = not reasons
    return report
