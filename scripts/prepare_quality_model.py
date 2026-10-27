"""获取固定版本的公开 BGE 小模型到指定目录，逐项验证 SHA-256。"""

import argparse
import hashlib
import json
from pathlib import Path
import urllib.parse
import urllib.request


REPOSITORY = "AI-ModelScope/bge-small-zh-v1.5"
REVISION = "b7b841ce366cce9454e10934fd8ad91dd4e5913d"
MODEL_SHA256 = "354763b9b1357bc9c44f62c6be2276321081ed2567773608c0d0785b61d5a026"
ALLOWED = frozenset(("1_Pooling/config.json", "config.json", "config_sentence_transformers.json", "model.safetensors",
                     "modules.json", "sentence_bert_config.json", "special_tokens_map.json", "tokenizer.json",
                     "tokenizer_config.json", "vocab.txt"))


def prepare(output):
    root = Path(output).resolve()
    with urllib.request.urlopen(f"https://modelscope.cn/api/v1/models/{REPOSITORY}/repo/files?Revision={REVISION}&Recursive=true", timeout=30) as response:
        metadata = json.load(response)
    records = [file for file in metadata["Data"]["Files"] if file["Path"] in ALLOWED and file["Type"] == "blob"]
    if {file["Path"] for file in records} != ALLOWED or sum(file["Size"] for file in records) > 150_000_000:
        raise ValueError("模型文件清单或大小不符合固定版本约定")
    model = next(file for file in records if file["Path"] == "model.safetensors")
    if model["Sha256"] != MODEL_SHA256:
        raise ValueError("固定权重摘要不一致，拒绝下载")
    files = []
    for file in records:
        target = root/file["Path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != file["Sha256"]:
            url = f"https://modelscope.cn/api/v1/models/{REPOSITORY}/repo?Revision={REVISION}&FilePath="+urllib.parse.quote(file["Path"])
            partial = target.with_suffix(target.suffix+".part")
            try:
                with urllib.request.urlopen(url, timeout=60) as response, partial.open("wb") as stream:
                    received = 0
                    while chunk := response.read(1024*1024):
                        received += len(chunk)
                        if received > file["Size"]:
                            raise ValueError("下载响应超过固定版本文件大小")
                        stream.write(chunk)
                if partial.stat().st_size != file["Size"] or hashlib.sha256(partial.read_bytes()).hexdigest() != file["Sha256"]:
                    raise ValueError("下载文件不符合版本摘要")
                partial.replace(target)
            finally:
                partial.unlink(missing_ok=True)
        files.append({"path": file["Path"], "bytes": file["Size"], "sha256": file["Sha256"]})
    manifest = {"source": REPOSITORY, "revision": REVISION, "license": "MIT", "files": files,
                "bytes": sum(file["bytes"] for file in files), "remote_code": False}
    (root/"download-manifest.json").write_text(json.dumps(manifest, indent=2)+"\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="dist/models/bge-small-zh-v1.5")
    args = parser.parse_args()
    print(json.dumps(prepare(args.output), indent=2))
