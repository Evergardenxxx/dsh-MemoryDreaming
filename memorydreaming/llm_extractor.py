"""DeepSeek 结构化记忆抽取适配器。

核心只接入 DeepSeek：使用其 OpenAI 兼容 ``chat/completions`` 接口，
通过 JSON object 模式和提示词约束返回候选。解析器仍保持可注入 callable，
方便测试而不把 SDK 引入项目；生产调用请使用 ``DeepSeekExtractor``。
模型只能返回候选，不能提供 event_id、speaker、scope 或权限字段。
"""

from __future__ import annotations

import json
import inspect
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .clock import ensure_utc
from .models import EpistemicStatus, EventRecord, MemoryDraft, MemoryType, Modality


EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["schema_version", "candidates"],
    "properties": {
        "schema_version": {"type": "string", "const": "1.0"},
        "candidates": {"type": "array", "maxItems": 32, "items": {
            "type": "object",
            "required": ["memory_type", "body", "candidate_key"],
            "properties": {
                "memory_type": {"enum": [item.value for item in MemoryType]},
                "body": {"type": "object"},
                "candidate_key": {"type": "string", "minLength": 1},
                "epistemic_status": {"enum": [item.value for item in EpistemicStatus]},
                "modality": {"enum": [item.value for item in Modality]},
                "importance": {"type": "number", "minimum": 0, "maximum": 1},
                "source_span": {"type": "array", "prefixItems": [{"type": "integer"}, {"type": "integer"}], "minItems": 2, "maxItems": 2},
                "occurred_start": {"type": ["string", "null"]},
                "occurred_end": {"type": ["string", "null"]},
                "valid_from": {"type": ["string", "null"]},
                "valid_to": {"type": ["string", "null"]},
            },
        }},
    },
}


class LLMExtractionError(ValueError):
    """模型返回不可接受候选时抛出的可重试错误。"""


class DeepSeekClient:
    """DeepSeek 官方 OpenAI 兼容接口的零依赖客户端。"""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "deepseek-chat",
        base_url: str = "https://api.deepseek.com",
        timeout: float = 30.0,
        max_tokens: int = 1200,
        opener: Any | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("DeepSeek API key 不能为空")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("DeepSeek model 不能为空")
        if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
            raise ValueError("DeepSeek base_url 必须是 http(s) URL")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("DeepSeek timeout 必须为正数")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 128:
            raise ValueError("DeepSeek max_tokens 必须至少为 128")
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.max_tokens = max_tokens
        self._opener = opener or urllib.request.urlopen

    @classmethod
    def from_env(cls) -> "DeepSeekClient":
        raw_timeout = os.environ.get("MEMORYDREAMING_DEEPSEEK_TIMEOUT", "30")
        raw_tokens = os.environ.get("MEMORYDREAMING_DEEPSEEK_MAX_TOKENS", "1200")
        try:
            timeout = float(raw_timeout)
            max_tokens = int(raw_tokens)
        except ValueError as exc:
            raise ValueError("DeepSeek timeout/max_tokens 环境变量格式错误") from exc
        return cls(
            os.environ.get("DEEPSEEK_API_KEY", ""),
            model=os.environ.get("MEMORYDREAMING_DEEPSEEK_MODEL", "deepseek-chat"),
            base_url=os.environ.get("MEMORYDREAMING_DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            timeout=timeout,
            max_tokens=max_tokens,
        )

    def __call__(self, prompt: str, *, schema: dict[str, Any]) -> str:
        # DeepSeek 的 JSON object 模式需要在 prompt 中明确要求 JSON；
        # 具体字段 schema 同时作为提示的一部分发送，避免依赖供应商私有 SDK。
        request_body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "你只能输出一个 JSON object，不要输出 Markdown。"},
                {"role": "user", "content": prompt + "\nJSON schema:\n" + json.dumps(schema, ensure_ascii=False)},
            ],
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        response = None
        try:
            response = self._opener(request, timeout=self.timeout)
            raw = response.read(2 * 1024 * 1024)
            payload = json.loads(raw.decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMExtractionError("DeepSeek 服务暂时不可用，请稍后重试") from exc
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            raise LLMExtractionError("DeepSeek 返回了不可解析的响应") from exc
        finally:
            if response is not None:
                response.close()

        if not isinstance(payload, dict):
            raise LLMExtractionError("DeepSeek 返回格式错误")
        if payload.get("error") is not None:
            raise LLMExtractionError("DeepSeek 请求被拒绝或额度不足")
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMExtractionError("DeepSeek 响应缺少 choices.message.content") from exc
        if not isinstance(content, str) or not content.strip():
            raise LLMExtractionError("DeepSeek 没有返回抽取内容")
        return content.strip()


def _moment(value: Any, name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LLMExtractionError(f"{name} 必须是 ISO8601 字符串或 null")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LLMExtractionError(f"{name} 不是有效的 ISO8601 时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LLMExtractionError(f"{name} 必须包含时区")
    return ensure_utc(parsed)


class LLMExtractor:
    """把无供应商依赖的 LLM JSON 输出转换为 ``MemoryDraft`` 列表。"""

    def __init__(
        self,
        provider: Callable[..., str | dict[str, Any]],
        *,
        version: str = "llm-v1",
        max_candidates: int = 32,
    ) -> None:
        if not callable(provider):
            raise TypeError("provider 必须是可调用对象")
        if not isinstance(version, str) or not version.strip():
            raise ValueError("version 不能为空")
        if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or not 1 <= max_candidates <= 32:
            raise ValueError("max_candidates 必须在 1 到 32 之间")
        self.provider = provider
        self.version = version
        self.max_candidates = max_candidates

    def prompt(self, event: EventRecord) -> str:
        return (
            "你是长期记忆抽取器。只从下面这条用户事件中抽取值得长期保留的候选。"
            "不要把假设、引用、虚构、愿望当作已发生事实；没有充分依据就返回空数组。"
            "memory_type 只能是 working/episodic/semantic/procedural。"
            "每个候选必须使用原文 Unicode 码点 source_span，candidate_key 在本事件内稳定。"
            "只能输出 JSON，不要输出解释。\n"
            f"事件说话者：{event.speaker}\n事件正文：{event.content}\n"
            "输出格式：{\"schema_version\":\"1.0\",\"candidates\":[...] }"
        )

    def extract(self, event: EventRecord) -> list[MemoryDraft]:
        prompt = self.prompt(event)
        try:
            try:
                signature = inspect.signature(self.provider)
                accepts_schema = (
                    "schema" in signature.parameters
                    or any(parameter.kind is inspect.Parameter.VAR_KEYWORD
                           for parameter in signature.parameters.values())
                )
            except (TypeError, ValueError):
                # 某些 C 扩展 callable 没有签名；按更丰富的协议尝试一次。
                accepts_schema = True
            if accepts_schema:
                raw = self.provider(prompt, schema=EXTRACTION_SCHEMA)
            else:
                raw = self.provider(prompt)
        except Exception as exc:  # provider 的网络/限流错误交给队列重试
            raise LLMExtractionError(f"抽取模型调用失败：{exc}") from exc

        if isinstance(raw, str):
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise LLMExtractionError("抽取模型没有返回有效 JSON") from exc
        else:
            payload = raw
        if not isinstance(payload, dict) or payload.get("schema_version") != "1.0":
            raise LLMExtractionError("抽取结果 schema_version 必须为 1.0")
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or len(candidates) > self.max_candidates:
            raise LLMExtractionError("抽取结果 candidates 必须是有限数组")

        drafts: list[MemoryDraft] = []
        for index, candidate in enumerate(candidates):
            if not isinstance(candidate, dict):
                raise LLMExtractionError(f"第 {index} 个候选不是对象")
            try:
                memory_type = MemoryType(candidate["memory_type"])
                body = candidate["body"]
                key = candidate["candidate_key"]
            except (KeyError, ValueError, TypeError) as exc:
                raise LLMExtractionError(f"第 {index} 个候选缺少合法的类型、正文或候选键") from exc
            if not isinstance(body, dict) or not isinstance(key, str) or not key.strip():
                raise LLMExtractionError(f"第 {index} 个候选正文或候选键非法")

            span = candidate.get("source_span")
            source_span: tuple[int, int] | None = None
            if span is None:
                raise LLMExtractionError(f"第 {index} 个候选必须提供 source_span")
            if span is not None:
                if (not isinstance(span, list) or len(span) != 2
                        or any(isinstance(value, bool) or not isinstance(value, int) for value in span)):
                    raise LLMExtractionError(f"第 {index} 个候选 source_span 非法")
                if not 0 <= span[0] < span[1] <= len(event.content):
                    raise LLMExtractionError(f"第 {index} 个候选 source_span 超出事件正文")
                source_span = (span[0], span[1])
            try:
                epistemic = EpistemicStatus(candidate.get("epistemic_status", EpistemicStatus.USER_REPORT.value))
                modality = Modality(candidate.get("modality", Modality.OCCURRED.value))
            except ValueError as exc:
                raise LLMExtractionError(f"第 {index} 个候选认识论或模态非法") from exc

            try:
                importance = float(candidate.get("importance", 0.5))
            except (TypeError, ValueError) as exc:
                raise LLMExtractionError(f"第 {index} 个候选 importance 非法") from exc
            drafts.append(MemoryDraft(
                memory_type=memory_type,
                body=body,
                candidate_key=key.strip(),
                epistemic_status=epistemic,
                modality=modality,
                occurred_start=_moment(candidate.get("occurred_start"), "occurred_start"),
                occurred_end=_moment(candidate.get("occurred_end"), "occurred_end"),
                valid_from=_moment(candidate.get("valid_from"), "valid_from"),
                valid_to=_moment(candidate.get("valid_to"), "valid_to"),
                importance=importance,
                source_span=source_span,
            ))
        return drafts


class DeepSeekExtractor(LLMExtractor):
    """使用 DeepSeek chat 模型的生产抽取器。"""

    def __init__(self, client: DeepSeekClient | None = None, **client_kwargs: Any) -> None:
        client = client or DeepSeekClient(**client_kwargs)
        super().__init__(client, version=f"deepseek:{client.model}:v1")
        self.client = client

    @classmethod
    def from_env(cls) -> "DeepSeekExtractor":
        return cls(client=DeepSeekClient.from_env())

