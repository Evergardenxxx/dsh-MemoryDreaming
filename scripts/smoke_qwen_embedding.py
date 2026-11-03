"""离线随机 Qwen3 架构检查；不下载权重，不评估真实语义质量。

安装可选 Qwen 依赖后执行：
    python scripts/smoke_qwen_embedding.py --output dist/qwen-smoke/report.json

临时生成只有一层、微型词表的随机 decoder，保留 1024 原生维度，验证
Sentence Transformers 的 last-token pooling、1024→32 截取、本地独立
worker 及临时 SQLite 投影。随机权重没有学过语言，不断言记忆相关性。
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import inspect
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

NATIVE_DIM = 1024
OUTPUT_DIM = 32
MAX_LENGTH = 64


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _valid_vectors(rows, *, count: int, dim: int) -> bool:
    return len(rows) == count and all(
        len(row) == dim and all(math.isfinite(value) for value in row)
        and math.isclose(math.hypot(*row), 1.0, abs_tol=1e-6)
        for row in rows
    )


def _make_fixture(root: Path) -> dict:
    """只从显式配置创建随机权重；所有加载只允许本地文件。"""
    import torch
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace
    from tokenizers.processors import TemplateProcessing
    from transformers import PreTrainedTokenizerFast, Qwen3Config, Qwen3Model
    from sentence_transformers import SentenceTransformer
    try:
        # 新版提供此路径；旧版保留相同模型模块接口。
        from sentence_transformers.sentence_transformer.modules import Normalize, Pooling, Transformer
    except ImportError:
        from sentence_transformers.models import Normalize, Pooling, Transformer

    raw = root / "random-decoder"
    prepared = root / "sentence-transformer"
    torch.manual_seed(37)
    config = Qwen3Config(
        vocab_size=64, hidden_size=NATIVE_DIM, intermediate_size=64,
        num_hidden_layers=1, num_attention_heads=16, num_key_value_heads=2,
        head_dim=64, max_position_embeddings=128, attention_dropout=0.0,
        pad_token_id=0, eos_token_id=1, use_cache=False,
    )
    model = Qwen3Model(config).eval()
    parameter_count = sum(parameter.numel() for parameter in model.parameters())
    model.save_pretrained(raw, safe_serialization=True)
    del model

    words = (
        "[PAD]", "[EOS]", "[UNK]", "Instruct", ":", "Given", "a", "conversation", "query",
        ",", "retrieve", "relevant", "memories", "that", "help", "answer", "the", "Query",
        "bedtime", "sea", "story", "mountain", "travel", "synthetic", "walk",
    )
    tokenizer = Tokenizer(WordLevel({word: index for index, word in enumerate(words)}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = Whitespace()
    tokenizer.post_processor = TemplateProcessing(single="$A [EOS]", special_tokens=[("[EOS]", 1)])
    wrapper = PreTrainedTokenizerFast(
        tokenizer_object=tokenizer, pad_token="[PAD]", eos_token="[EOS]", unk_token="[UNK]",
        model_max_length=MAX_LENGTH, padding_side="left",
    )
    wrapper.save_pretrained(raw)

    local = {"local_files_only": True, "trust_remote_code": False}
    transformer_options = {"model_kwargs": local, "config_kwargs": local, "max_seq_length": MAX_LENGTH}
    # ST 5.7 renamed tokenizer_kwargs to processor_kwargs. Support both
    # without requesting remote code or falling back to a hosted model.
    processor_option = "processor_kwargs" if "processor_kwargs" in inspect.signature(Transformer).parameters else "tokenizer_kwargs"
    transformer_options[processor_option] = local
    transformer = Transformer(str(raw), **transformer_options)
    sentence_model = SentenceTransformer(
        modules=[transformer, Pooling(NATIVE_DIM, pooling_mode="lasttoken", include_prompt=True), Normalize()],
        device="cpu",
    )
    sentence_model.save(str(prepared), create_model_card=False)
    del sentence_model
    return {"path": prepared, "parameters": parameter_count,
            "safetensors_bytes": sum(file.stat().st_size for file in prepared.rglob("*.safetensors"))}


def run_smoke() -> dict:
    # These flags also reach the child model worker. No model repository IDs
    # or network endpoints are accepted by this smoke script.
    for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
        os.environ[key] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        os.environ[key] = "2"

    import torch
    import sentence_transformers
    import tokenizers
    import transformers
    from memorydreaming import MemoryItem, MemoryRuntime, MemoryType, Scope
    from memorydreaming.config import Settings
    from memorydreaming.models import new_id
    from memorydreaming.retrieval.embedding import embedder_from_settings
    from memorydreaming.retrieval.profiles import QWEN3_MEMORY_QUERY_PREFIX

    torch.set_num_threads(2)
    started = time.perf_counter()
    checks = {}
    # The fixture directory outlives every runtime and worker, including on
    # failure, so Windows can remove the model files after the child exits.
    with tempfile.TemporaryDirectory(prefix="memorydreaming-qwen-random-") as directory:
        root = Path(directory)
        fixture = _make_fixture(root)
        settings = Settings(
            embedding_provider="sentence-transformers", embedding_profile="qwen3-embedding-0.6b",
            embedding_model_path=str(fixture["path"]), embedding_revision="random-qwen3-architecture-fixture-v1",
            embedding_dim=OUTPUT_DIM, embedding_max_seq_length=MAX_LENGTH, embedding_batch_size=2,
            embedding_timeout_seconds=30,
        )
        direct = embedder_from_settings(settings)
        _require(direct.cache_signature["query_prefix"] == QWEN3_MEMORY_QUERY_PREFIX,
                 "Qwen profile did not supply its memory query instruction")
        _require(direct.cache_signature["document_prefix"] == "", "profile added a document prompt")
        texts = ["bedtime sea story", "mountain travel"]
        documents = direct.embed_documents(texts)
        query = direct.embed_query(texts[0])
        _require(_valid_vectors(documents, count=2, dim=OUTPUT_DIM), "direct document vectors invalid")
        _require(_valid_vectors([query], count=1, dim=OUTPUT_DIM), "direct query vector invalid")
        _require(direct._native_dim == NATIVE_DIM, "fixture did not encode its full native dimension")
        prefix_distance = math.dist(query, documents[0])
        _require(prefix_distance > 1e-6, "query instruction had no numerical effect")
        checks.update(native_dimension=NATIVE_DIM, output_dimension=OUTPUT_DIM,
                      direct_vectors_finite_unit_norm=True, query_document_prefix_distinct=True,
                      query_document_l2_distance=round(prefix_distance, 8))

        isolated = embedder_from_settings(settings, isolated=True)
        process = None
        try:
            worker_documents = isolated.embed_documents(texts)
            worker_query = isolated.embed_query(texts[0])
            _require(_valid_vectors(worker_documents, count=2, dim=OUTPUT_DIM), "worker document vectors invalid")
            _require(_valid_vectors([worker_query], count=1, dim=OUTPUT_DIM), "worker query vector invalid")
            delta = max(abs(value - actual) for expected, actual_row in
                        zip(documents + [query], worker_documents + [worker_query])
                        for value, actual in zip(expected, actual_row))
            _require(delta < 1e-5, "direct and isolated decoder outputs differ")
            process = isolated._process
            _require(process is not None and process.poll() is None, "model worker is not alive")
            checks.update(isolated_worker_started=True, isolated_vectors_finite_unit_norm=True,
                          direct_worker_max_abs_difference=round(delta, 10))

            scope = Scope("random-fixture-user", "random-fixture-character")
            foreign = Scope("different-fixture-user", "random-fixture-character")
            database = root / "synthetic.db"
            moment = datetime.now(timezone.utc)
            expected_ids = set()
            runtime_settings = replace(settings, db_path=database)
            with MemoryRuntime(settings=runtime_settings) as runtime:
                owned_embedder = runtime.vectors._embedder
                _require(owned_embedder.cache_signature == isolated.cache_signature,
                         "runtime and direct factory configurations differ")
                with runtime.db.transaction():
                    for text in texts:
                        memory_id = new_id("mem")
                        expected_ids.add(memory_id)
                        runtime.db.repositories.memories.insert(MemoryItem(
                            memory_id, MemoryType.EPISODIC, scope, {"what_happened": text},
                            source_speaker="user", recorded_at=moment,
                        ))
                _require(runtime.rebuild_vectors(scope) == 2, "synthetic vector build incomplete")
                owned_process = owned_embedder._process
                _require(owned_process is not None and owned_process.poll() is None,
                         "runtime did not start its owned worker")
            _require(owned_process.poll() is not None, "runtime did not close its owned worker")
            # Reopen SQLite as a new runtime. A permissive threshold checks
            # persistence and filtering only, never the meaning of random scores.
            with MemoryRuntime(settings=runtime_settings) as reopened:
                result = reopened.vectors.search_with_status(scope, texts[0], min_score=-1.0, limit=2)
                _require({hit.memory_id for hit in result.hits} == expected_ids, "persisted projection mismatch")
                _require(not result.degraded_reasons, "synthetic projection unexpectedly degraded")
                _require(not reopened.vectors.search(foreign, texts[0], min_score=-1.0), "scope filtering failed")
                _require(reopened.vectors.count() == 2, "persisted vector count mismatch")
                reopened_process = reopened.vectors._embedder._process
            _require(reopened_process is not None and reopened_process.poll() is not None,
                     "reopened runtime did not close its owned worker")
            checks.update(synthetic_sqlite_reopened=True, synthetic_vectors_persisted=2,
                          foreign_scope_filtered=True, production_profile_factory=True,
                          runtime_owned_workers_closed=True)
        finally:
            # Injected embedders belong to the caller, not MemoryRuntime.
            isolated.close()
        _require(process is not None and process.poll() is not None, "model worker did not exit")
        checks["isolated_worker_closed"] = True
        fixture_info = {key: value for key, value in fixture.items() if key != "path"}

    return {
        "status": "ok", "evidence_kind": "random_architecture_fixture", "real_qwen_weights": False,
        "quality_accepted": False, "network_downloads": False, "user_database_used": False,
        "semantic_labels_evaluated": False,
        "fixture": {"architecture": "Qwen3Model", "hidden_size": NATIVE_DIM, "vocab_size": 64,
                    "layers": 1, "intermediate_size": 64, "seed": 37, "pooling": "lasttoken",
                    "max_seq_length": MAX_LENGTH, "batch_size": 2, "cpu_threads": 2, **fixture_info},
        "libraries": {"torch": torch.__version__, "transformers": transformers.__version__,
                      "sentence_transformers": sentence_transformers.__version__, "tokenizers": tokenizers.__version__},
        "checks": checks, "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        report = run_smoke()
    except Exception as exc:
        report = {"status": "unavailable", "evidence_kind": "random_architecture_fixture",
                  "real_qwen_weights": False, "quality_accepted": False,
                  "error_code": "OPTIONAL_DEPENDENCY_REQUIRED" if isinstance(exc, ImportError) else "SMOKE_CHECK_FAILED",
                  "exception_type": type(exc).__name__}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
