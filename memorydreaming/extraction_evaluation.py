"""以外部标签评估抽取候选；不创建运行时或写入长期记忆。"""

from __future__ import annotations

import json
import hashlib
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path

from .formation import MemoryFormationPolicy
from .llm_extractor import LLMExtractionError, LLMExtractor
from .models import EventRecord, MemoryDraft, Scope


DEFAULT_DATASET = Path(__file__).with_name("data") / "extraction-zh-v1.json"
SCORER_VERSION = "literal-evidence-match-v1"


def dataset_fingerprint(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def load_dataset(path=DEFAULT_DATASET) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("dataset_id"), str):
        raise ValueError("评估集缺少 dataset_id")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases or len(cases) > 10000:
        raise ValueError("评估集 cases 必须包含 1..10000 项")
    ids = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or case["id"] in ids:
            raise ValueError("评估 case.id 必须为唯一字符串")
        ids.add(case["id"])
        if not isinstance(case.get("content"), str) or not case["content"]:
            raise ValueError("评估 case.content 必须为非空字符串")
        if case.get("speaker", "user") not in ("user", "character", "assistant", "system"):
            raise ValueError("评估 case.speaker 非法")
        if not isinstance(case.get("forbid_occurred", False), bool):
            raise ValueError("评估 case.forbid_occurred 必须为布尔值")
        if not isinstance(case.get("labels"), list) or len(case["labels"]) > 32:
            raise ValueError("评估 case.labels 必须是最多 32 项的数组")
        for label in case["labels"]:
            if (not isinstance(label, dict) or label.get("memory_type") not in
                    ("semantic", "working", "episodic", "procedural")
                    or not isinstance(label.get("body"), dict) or not label["body"]
                    or label.get("modality") not in ("occurred", "intention", "hypothetical", "quoted", "fictional")
                    or not isinstance(label.get("evidence"), str) or not label["evidence"]
                    or label["evidence"] not in case["content"]):
                raise ValueError("评估标签必须有合法类型、正文匹配字段、模态和原文 evidence")
    return data


def _normal(value):
    if isinstance(value, str):
        return " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    if isinstance(value, list):
        return [_normal(item) for item in value]
    return value


def _matches(draft: MemoryDraft, label: dict, content: str) -> bool:
    if draft.memory_type.value != label["memory_type"] or draft.modality.value != label["modality"]:
        return False
    if label.get("epistemic_status") and draft.epistemic_status.value != label["epistemic_status"]:
        return False
    if any(_normal(draft.body.get(key)) != _normal(value) for key, value in label["body"].items()):
        return False
    # 标注片段必须被候选来源区间实际包含，不能仅用“任意合法下标”拿满分。
    return draft.source_span is not None and label["evidence"] in content[slice(*draft.source_span)]


def _matched_count(drafts, labels, content):
    # 一对一最大匹配；一个泛化候选不能同时算作多个独立事实。
    assigned = {}

    def assign(index, seen):
        for target, label in enumerate(labels):
            if target in seen or not _matches(drafts[index], label, content):
                continue
            seen.add(target)
            if target not in assigned or assign(assigned[target], seen):
                assigned[target] = index
                return True
        return False

    return sum(assign(index, set()) for index in range(len(drafts)))


def serialize_drafts(drafts, event):
    result = []
    for draft in drafts:
        item = {"memory_type": draft.memory_type.value, "body": draft.body,
                "candidate_key": draft.candidate_key, "epistemic_status": draft.epistemic_status.value,
                "modality": draft.modality.value, "importance": draft.importance,
                "occurred_precision": draft.occurred_precision,
                "source_span": list(draft.source_span) if draft.source_span is not None else None}
        for key in ("occurred_start", "occurred_end", "valid_from", "valid_to"):
            value = getattr(draft, key)
            if key == "occurred_start" and value is None and draft.occurred_precision != "unknown":
                value = event.occurred_at  # 与规则候选落库时的默认事件时间保持一致。
            item[key] = value.isoformat() if value is not None else None
        result.append(item)
    return {"schema_version": "1.0", "candidates": result}


class ReplayExtractor:
    """重放保存的模型候选，使用与生产相同的严格解析器。"""

    def __init__(self, path, dataset_id, *, dataset_sha256=None):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("dataset_id") != dataset_id:
            raise ValueError("预测文件 dataset_id 不匹配")
        if dataset_sha256 is not None and data.get("dataset_sha256") != dataset_sha256:
            raise ValueError("预测文件的评估集内容指纹不匹配")
        if not isinstance(data.get("predictions"), dict):
            raise ValueError("预测文件缺少 predictions")
        self.predictions = data["predictions"]
        self.version = "replay:" + str(data.get("extractor_version", "unknown"))
        self.parser = LLMExtractor(lambda prompt: None)

    def extract(self, event):
        record = self.predictions.get(event.event_id)
        if not isinstance(record, dict) or "payload" not in record:
            raise LLMExtractionError("预测缺失或原调用失败", code="MISSING_PREDICTION", retryable=False)
        return self.parser.parse(event, record["payload"])


def run_extraction_evaluation(extractor, *, dataset_path=DEFAULT_DATASET, predictions_output=None) -> dict:
    data = load_dataset(dataset_path)
    fingerprint = dataset_fingerprint(data)
    totals = Counter()
    results, predictions = [], {}
    moment = datetime.fromisoformat("2026-10-27T12:00:00+08:00")
    policy = MemoryFormationPolicy()
    for case in data["cases"]:
        event = EventRecord(
            event_id=case["id"], scope=Scope("eval_user", "eval_character", domain=case.get("domain", "real")),
            conversation_id="eval:" + case["id"], speaker=case.get("speaker", "user"),
            content=case["content"], occurred_at=moment, recorded_at=moment,
        )
        labels = case["labels"]
        totals["expected"] += len(labels)
        drafts, code = [], None
        try:
            raw_drafts = extractor.extract(event)
            if not isinstance(raw_drafts, list) or len(raw_drafts) > 32:
                raise ValueError("invalid candidate list")
            drafts = [policy.validate(event, draft) for draft in raw_drafts]
            if any(draft.source_span is None or not 0 <= draft.source_span[0] < draft.source_span[1] <= len(event.content)
                   for draft in drafts):
                raise ValueError("invalid evidence span")
            predictions[case["id"]] = {"payload": serialize_drafts(drafts, event)}
        except Exception as exc:
            drafts = []
            code = exc.code if isinstance(exc, LLMExtractionError) else "EXTRACTION_FAILED"
            predictions[case["id"]] = {"error_code": code}
            totals["errors"] += 1
        matched = _matched_count(drafts, labels, event.content)
        false_positives = len(drafts) - matched
        false_negatives = len(labels) - matched
        unsafe = sum(draft.modality.value == "occurred" for draft in drafts) if case.get("forbid_occurred") else 0
        if case.get("forbid_occurred"):
            totals["negative_cases"] += 1
            totals["false_memory_cases"] += int(unsafe > 0)
            totals["negative_case_errors"] += int(code is not None)
        passed = not code and not false_positives and not false_negatives and not unsafe
        totals.update({"predicted": len(drafts), "matched": matched, "false_positives": false_positives,
                       "false_negatives": false_negatives, "unsafe_occurred": unsafe, "passed": int(passed)})
        results.append({"id": case["id"], "tag": case.get("tag", "unspecified"), "passed": passed,
                        "expected": len(labels), "predicted": len(drafts), "matched": matched,
                        "false_positives": false_positives, "false_negatives": false_negatives,
                        "unsafe_occurred": unsafe, "error_code": code})
    count = len(results)
    report = {"dataset_id": data["dataset_id"], "dataset_sha256": fingerprint,
              "scorer_version": SCORER_VERSION, "extractor_version": extractor.version,
              "case_count": count, "passed": totals["passed"], "errors": totals["errors"],
              "expected": totals["expected"], "predicted": totals["predicted"], "matched": totals["matched"],
              "false_positives": totals["false_positives"], "false_negatives": totals["false_negatives"],
              "precision": totals["matched"] / totals["predicted"] if totals["predicted"] else None,
              "recall": totals["matched"] / totals["expected"] if totals["expected"] else None,
              "case_accuracy": totals["passed"] / count,
              "negative_cases": totals["negative_cases"], "false_memory_cases": totals["false_memory_cases"],
              "negative_case_errors": totals["negative_case_errors"],
              "false_memory_case_rate": (totals["false_memory_cases"] / totals["negative_cases"]
                                         if totals["negative_cases"] else None),
              "results": results, "execution_complete": totals["errors"] == 0, "quality_accepted": False,
              "limitations": [(
                  "项目内置合成中文回归集，尚未经独立人工复核，不代表真实对话分布"
                  if Path(dataset_path).resolve() == DEFAULT_DATASET.resolve()
                  else "外部标注文件未经本评估器核实独立性，不能自动视为真实对话质量证据"),
                              "正文按标注字段字面匹配；同义改写需人工复核，不能据此判断事实真假",
                              "false_memory 仅衡量标注禁止 occurred 的反例；不等于最终回答幻觉率",
                              "空标签采用保守弃记策略；合法的引述/愿望候选也可能被计为标注外输出"]}
    if predictions_output is not None:
        Path(predictions_output).write_text(json.dumps({
            "dataset_id": data["dataset_id"], "dataset_sha256": fingerprint,
            "extractor_version": extractor.version, "predictions": predictions,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
