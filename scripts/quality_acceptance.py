"""校准/验收入口；不读取用户数据库，未达人工复核门槛返回 2。"""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memorydreaming.quality_acceptance import calibrate_embedding, evaluate_embedding_acceptance, evaluate_extraction_acceptance


def export_harness(dataset_path, output):
    """仅输出可在已登录应用发起的资料，不调用应用或从应用读取凭据。"""
    from memorydreaming.extraction_evaluation import dataset_fingerprint, load_dataset
    from memorydreaming.llm_extractor import EXTRACTION_SCHEMA, LLMExtractor
    from memorydreaming.models import EventRecord, Scope
    dataset = load_dataset(dataset_path)
    directory = Path(output)
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("评测导出目录必须为空，避免覆盖已经收集的模型返回")
    directory.mkdir(parents=True, exist_ok=True)
    extractor = LLMExtractor(lambda prompt: None)
    prompts = []
    timestamp = datetime.fromisoformat("2026-10-27T12:00:00+08:00")
    for index, case in enumerate(dataset["cases"]):
        event = EventRecord(case["id"], Scope("eval_user", "eval_character", domain=case.get("domain", "real")),
                            "eval:"+case["id"], case.get("speaker", "user"), case["content"], timestamp, recorded_at=timestamp)
        # 非用户事件按生产策略本地返回空候选；不让模型把角色自述升级为用户事实。
        skip = event.speaker != "user"
        prompt = extractor.prompt(event)+"\nJSON schema:\n"+json.dumps(EXTRACTION_SCHEMA, ensure_ascii=False)
        file = f"case-{index+1:04d}.txt"
        (directory/file).write_text(prompt+"\n", encoding="utf-8")
        prompts.append({"id": case["id"], "prompt_file": file, "production_skips_provider": skip})
    manifest = {"format": "memorydreaming.harness-extraction-eval.v1", "dataset_id": dataset["dataset_id"],
                "dataset_sha256": dataset_fingerprint(dataset), "cases": prompts,
                "instructions": ["在专用临时对话中逐条提交 prompt_file，记录所用模型与应用版本；不要混入私人历史。",
                                 "原样保存 JSON 候选，禁止修改结果来匹配标签。非用户事件使用生产跳过结果。",
                                 "汇总为 predictions.json 后运行 extraction-replay；离线重放评分不能自行证明模型来源或人工复核。"]}
    (directory/"manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    predictions = {"dataset_id": dataset["dataset_id"], "dataset_sha256": manifest["dataset_sha256"],
                   "extractor_version": "manual-harness-export-unverified", "predictions": {
                       case["id"]: ({"payload": {"schema_version": "1.0", "candidates": []}} if case["production_skips_provider"]
                                    else {"error_code": "NOT_RUN"}) for case in prompts}}
    (directory/"predictions.json").write_text(json.dumps(predictions, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    return {"exported": len(prompts), "dataset_sha256": manifest["dataset_sha256"],
            "provider_called": False, "accepted": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    for mode in ("embedding", "extraction-replay", "extraction-deepseek"):
        command = sub.add_parser(mode)
        command.add_argument("--calibration", required=True)
        command.add_argument("--evaluation", required=True)
        command.add_argument("--output", required=True)
        command.add_argument("--review")
        if mode == "embedding":
            command.add_argument("--model-path", required=True)
            command.add_argument("--revision", required=True)
            command.add_argument("--dim", type=int, default=512)
            command.add_argument("--query-prefix", default="为这个句子生成表示以用于检索相关文章：")
            command.add_argument("--k", type=int, default=3)
            command.add_argument("--calibration-artifact", required=True)
        if mode == "extraction-replay":
            command.add_argument("--predictions", required=True)
    export = sub.add_parser("export-harness")
    export.add_argument("--dataset", required=True)
    export.add_argument("--output", required=True)
    split = sub.add_parser("split-diagnostic")
    split.add_argument("--kind", choices=("embedding", "extraction"), required=True)
    split.add_argument("--dataset", required=True)
    split.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    if args.mode == "split-diagnostic":
        from memorydreaming.embedding_evaluation import load_embedding_dataset
        from memorydreaming.extraction_evaluation import load_dataset
        dataset = load_embedding_dataset(args.dataset) if args.kind == "embedding" else load_dataset(args.dataset)
        field = "queries" if args.kind == "embedding" else "cases"
        output = Path(args.output)
        output.mkdir(parents=True, exist_ok=True)
        for role, offset in (("calibration", 0), ("evaluation", 1)):
            part = {**dataset, "dataset_id": dataset["dataset_id"]+"-"+role+"-diagnostic",
                    field: dataset[field][offset::2]}
            if not part[field]:
                raise ValueError("诊断切分至少需要两条样例")
            (output/(role+".json")).write_text(json.dumps(part, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        print(json.dumps({"split": "alternating_diagnostic_only", "independently_reviewed": False,
                          "labels_created_or_changed": False}))
        return 0
    if args.mode == "export-harness":
        print(json.dumps(export_harness(args.dataset, args.output), ensure_ascii=False, indent=2))
        return 0
    review = json.loads(Path(args.review).read_text(encoding="utf-8")) if args.review else None
    if args.mode == "embedding":
        from memorydreaming.retrieval.embedding import SentenceTransformerEmbedder
        embedder = SentenceTransformerEmbedder(args.model_path, revision=args.revision, dim=args.dim,
                                                query_prefix=args.query_prefix)
        artifact_path = Path(args.calibration_artifact)
        if artifact_path.exists():
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        else:
            artifact = calibrate_embedding(embedder, calibration_path=args.calibration, evaluation_path=args.evaluation, k=args.k)
            artifact_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        if artifact.get("status") != "calibrated":
            report = artifact
        else:
            report = evaluate_embedding_acceptance(embedder, calibration_path=args.calibration, evaluation_path=args.evaluation,
                                                    calibration_artifact=artifact, review=review)
    else:
        from memorydreaming.extraction_evaluation import ReplayExtractor, dataset_fingerprint, load_dataset
        if args.mode == "extraction-deepseek":
            if not os.environ.get("DEEPSEEK_API_KEY"):
                report = {"status": "needs_credentials", "acceptance": {"accepted": False,
                            "reasons": ["DEEPSEEK_API_KEY_REQUIRED"]}, "provider_called": False}
                Path(args.output).write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
                print(json.dumps(report, indent=2))
                return 2
            from memorydreaming.llm_extractor import DeepSeekExtractor
            extractor = DeepSeekExtractor.from_env()
        else:
            dataset = load_dataset(args.evaluation)
            extractor = ReplayExtractor(args.predictions, dataset["dataset_id"], dataset_sha256=dataset_fingerprint(dataset))
        report = evaluate_extraction_acceptance(extractor, calibration_path=args.calibration,
                                                evaluation_path=args.evaluation, review=review)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("acceptance", {}).get("accepted") else 2


if __name__ == "__main__":
    raise SystemExit(main())
