"""稳定断言表示。不使用抽取器版本、候选次序或向量作为遗忘键。"""

from __future__ import annotations

import json
from dataclasses import asdict
import unicodedata
from typing import Any

from .models import CoreKind, EventRecord, MemoryItem, MemoryType, Scope


def normalize(value: Any) -> Any:
    if isinstance(value, str):
        return "".join(unicodedata.normalize("NFKC", value).casefold().split())
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [normalize(item) for item in value]
    return value


def assertion_material(memory: MemoryItem) -> str:
    body = memory.body
    if memory.memory_type is MemoryType.SEMANTIC:
        body = {key: body.get(key, "") for key in ("subject", "predicate", "obj", "scope_note")}
    return json.dumps(
        [memory.memory_type.value, memory.modality.value, normalize(body)],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def core_scope_key(scope: Scope, kind: CoreKind) -> str:
    return json.dumps(
        [scope.user_id, scope.character_id, scope.domain,
         scope.relationship_id if kind is CoreKind.RELATIONSHIP else None],
        ensure_ascii=False, separators=(",", ":"),
    )


def relearning_material(memory: MemoryItem) -> str:
    """将授权绑定完整正文和新来源，不能靠沿用记录 ID 换回旧依据。"""
    return json.dumps(
        [memory.body, memory.memory_type.value, memory.modality.value,
         memory.epistemic_status.value, memory.source_speaker,
         memory.source_event_ids, memory.source_spans],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )


def confirmation_material(event: EventRecord) -> str:
    return json.dumps(asdict(event), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str, allow_nan=False)
