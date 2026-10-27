"""Pinned local DeepSeek tokenization; remote request template fidelity stays explicit.

Only local JSON tokenizer/config/manifest files are loaded. No model code, API,
credentials, or runtime downloads are involved. Run ``python -m ...tokenization``
as a bounded one-shot subprocess from the native host.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


class TokenizationError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _json_file(path, limit):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise TokenizationError("TOKENIZER_ASSET_INVALID", "Local tokenizer asset missing or exceeds size limit")
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise TokenizationError("TOKENIZER_ASSET_INVALID", "Local tokenizer asset is not valid JSON") from None
    return raw, value


class LocalDeepSeekTokenizer:
    """Exact text encoding; complete request fidelity is a separate property."""

    mode = "exact"

    def __init__(self, directory):
        self.directory = Path(directory).expanduser().resolve()
        _, manifest = _json_file(self.directory / "memorydreaming-tokenizer.json", 16384)
        if (not isinstance(manifest, dict) or manifest.get("format") != "memorydreaming.deepseek-tokenizer"
                or manifest.get("version") != 1 or not isinstance(manifest.get("revision"), str)
                or not manifest["revision"].strip()):
            raise TokenizationError("TOKENIZER_MANIFEST_INVALID", "A pinned tokenizer manifest is required")
        for name in ("model_ids", "provider_ids"):
            values = manifest.get(name)
            if (not isinstance(values, list) or not 1 <= len(values) <= 32
                    or any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in values)):
                raise TokenizationError("TOKENIZER_MANIFEST_INVALID", "Explicit provider and model IDs are required")
        tokenizer_raw, _ = _json_file(self.directory / "tokenizer.json", 32 * 1024 * 1024)
        config_raw, config = _json_file(self.directory / "tokenizer_config.json", 256 * 1024)
        for name, raw in (("tokenizer", tokenizer_raw), ("config", config_raw)):
            expected = manifest.get(name + "_sha256")
            if not isinstance(expected, str) or len(expected) != 64 or hashlib.sha256(raw).hexdigest() != expected:
                raise TokenizationError("TOKENIZER_HASH_MISMATCH", "Local tokenizer/config hash does not match its manifest")
        try:
            from tokenizers import Tokenizer
        except ImportError:
            raise TokenizationError("TOKENIZER_DEPENDENCY_MISSING", "Install the optional tokenizers dependency in the selected Python environment") from None
        try:
            self.tokenizer = Tokenizer.from_str(tokenizer_raw.decode("utf-8"))
            self.tokenizer.no_truncation()
            self.tokenizer.no_padding()
        except Exception:
            raise TokenizationError("TOKENIZER_ASSET_INVALID", "Local tokenizer could not be loaded") from None
        self.manifest, self.config = manifest, config

    def count(self, text):
        if not isinstance(text, str):
            raise TokenizationError("TOKENIZER_INPUT_INVALID", "Tokenizer text must be a string")
        if len(text.encode("utf-8")) > 16 * 1024 * 1024:
            raise TokenizationError("TOKENIZER_INPUT_TOO_LARGE", "Tokenizer text exceeds size limit")
        return len(self.tokenizer.encode(text, add_special_tokens=False).ids)

    def count_template(self, messages, *, add_generation_prompt=True):
        template = self.config.get("chat_template")
        if not isinstance(template, str) or not template:
            raise TokenizationError("TOKENIZER_TEMPLATE_MISSING", "Local tokenizer has no pinned chat template")
        if (not isinstance(messages, list) or len(messages) > 10000
                or any(not isinstance(m, dict) or m.get("role") not in ("system", "user", "assistant", "tool")
                       or not isinstance(m.get("content"), str) for m in messages)):
            raise TokenizationError("TOKENIZER_PROJECTION_UNSUPPORTED", "Local chat-template count supports text messages only")
        try:
            from jinja2.sandbox import ImmutableSandboxedEnvironment
            env = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True)
            def token(name):
                value = self.config.get(name)
                return value.get("content", "") if isinstance(value, dict) else value or ""
            rendered = env.from_string(template).render(messages=messages,
                add_generation_prompt=add_generation_prompt,
                bos_token=token("bos_token"), eos_token=token("eos_token"),
                pad_token=token("pad_token"), unk_token=token("unk_token"))
        except ImportError:
            raise TokenizationError("TOKENIZER_DEPENDENCY_MISSING", "Install optional jinja2 to count the local chat template") from None
        except Exception:
            raise TokenizationError("TOKENIZER_TEMPLATE_INVALID", "The pinned local chat template could not render this request") from None
        return self.count(rendered)

    def check_binding(self, provider, model):
        if provider not in self.manifest["provider_ids"] or model not in self.manifest["model_ids"]:
            raise TokenizationError("TOKENIZER_MODEL_MISMATCH", "Tokenizer manifest does not authorize this provider/model ID")

    def count_request(self, request):
        self.check_binding(request.get("provider"), request.get("model"))
        wire = request.get("projected")
        if not isinstance(wire, dict) or request.get("projection") != "deepseek-messages-text-v1":
            raise TokenizationError("TOKENIZER_PROJECTION_UNSUPPORTED", "Only the explicit text Messages projection is supported")
        if (set(wire) - {"model", "messages", "system", "tools"}
                or wire.get("model", request["model"]) != request["model"]
                or not isinstance(wire.get("messages"), list)
                or ("system" in wire and not isinstance(wire["system"], str))):
            raise TokenizationError("TOKENIZER_PROJECTION_UNSUPPORTED", "Invalid projected Messages input")
        def validate_blocks(blocks):
            if not isinstance(blocks, list):
                return False
            for block in blocks:
                if not isinstance(block, dict):
                    return False
                kind = block.get("type")
                if kind == "text" and isinstance(block.get("text"), str):
                    continue
                if kind == "thinking" and isinstance(block.get("thinking"), str) and "signature" not in block:
                    continue
                if kind == "tool_use" and isinstance(block.get("id"), str) and isinstance(block.get("name"), str) and isinstance(block.get("input"), dict):
                    continue
                if kind == "tool_result" and isinstance(block.get("tool_use_id"), str) and validate_blocks(block.get("content")):
                    continue
                if kind in ("tool_addition", "tool_removal") and isinstance(block.get("tool"), dict) and block["tool"].get("type") == "tool_reference":
                    continue
                return False
            return True
        if any(not isinstance(message, dict) or message.get("role") not in ("system", "user", "assistant")
               or not validate_blocks(message.get("content")) for message in wire["messages"]):
            raise TokenizationError("TOKENIZER_PROJECTION_UNSUPPORTED", "Projected image/file/replay or invalid content is unsupported")
        # Count actual wire content and tool definitions using the pinned tokenizer.
        # Public model files do not specify the hosted Messages server template.
        encoded = json.dumps(wire, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        return {"input_tokens": self.count(encoded), "count_mode": "estimated",
                "text_tokenization": "exact", "template_fidelity": "wire-json-estimate",
                "tokenizer_revision": self.manifest["revision"],
                "tokenizer_sha256": self.manifest["tokenizer_sha256"],
                "projection": "deepseek-messages-text-v1"}


def create_manifest(directory, *, provider_ids, model_ids, revision):
    """Explicit trusted operator step; does not assert that aliases match weights."""
    target = Path(directory).expanduser().resolve()
    tokenizer, _ = _json_file(target / "tokenizer.json", 32 * 1024 * 1024)
    config, _ = _json_file(target / "tokenizer_config.json", 256 * 1024)
    manifest = {"format": "memorydreaming.deepseek-tokenizer", "version": 1,
                "provider_ids": provider_ids, "model_ids": model_ids, "revision": revision,
                "tokenizer_sha256": hashlib.sha256(tokenizer).hexdigest(),
                "config_sha256": hashlib.sha256(config).hexdigest()}
    path = target / "memorydreaming-tokenizer.json"
    if path.exists():
        raise TokenizationError("TOKENIZER_MANIFEST_EXISTS", "Refusing to overwrite a pinned tokenizer manifest")
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pinned local tokenizer; never downloads weights or calls model APIs")
    parser.add_argument("--directory", required=True)
    parser.add_argument("--create-manifest", action="store_true")
    parser.add_argument("--provider", action="append")
    parser.add_argument("--model", action="append")
    parser.add_argument("--revision")
    args = parser.parse_args(argv)
    try:
        if args.create_manifest:
            if not args.provider or not args.model or not args.revision:
                parser.error("Manifest requires --provider, --model and --revision")
            result = create_manifest(args.directory, provider_ids=args.provider, model_ids=args.model, revision=args.revision)
        else:
            line = sys.stdin.buffer.read(16 * 1024 * 1024 + 1)
            if len(line) > 16 * 1024 * 1024:
                raise TokenizationError("TOKENIZER_INPUT_TOO_LARGE", "Tokenizer input exceeds size limit")
            request = json.loads(line)
            tokenizer = LocalDeepSeekTokenizer(args.directory)
            if request.get("kind") == "text":
                result = {"tokens": tokenizer.count(request["text"]), "count_mode": "exact", "scope": "text-only"}
            elif request.get("kind") == "template":
                tokenizer.check_binding(request.get("provider"), request.get("model"))
                result = {"tokens": tokenizer.count_template(request["messages"]), "count_mode": "exact",
                          "scope": "pinned-local-template", "remote_usage_verified": False}
            else:
                result = tokenizer.count_request(request)
        print(json.dumps({"ok": True, "data": result}, ensure_ascii=False, separators=(",", ":")))
        return 0
    except TokenizationError as error:
        print(json.dumps({"ok": False, "error": {"code": error.code, "message": str(error)}}))
        return 2
    except Exception:
        print(json.dumps({"ok": False, "error": {"code": "TOKENIZER_INPUT_INVALID", "message": "Local tokenizer request failed"}}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
