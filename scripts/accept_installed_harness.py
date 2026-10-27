"""只读安装版公开运行时代码，在隔离目录验收原生插件，不读取用户配置/凭据。"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile


def extract_runtime(asar, destination):
    seeds = ["@deepseek-ai/dsh-agent-loop", "@deepseek-ai/dsh-session-projection",
             "@deepseek-ai/dsh-session-persistence-jsonl", "@deepseek-ai/dsh-compaction-basic",
             "@deepseek-ai/dsh-token-meter"]
    versions = {}
    with asar.open("rb") as stream:
        lengths = struct.unpack("<IIII", stream.read(16))
        tree = json.loads(stream.read(lengths[3]))
        base = 8+lengths[1]
        modules = tree["files"]["dsh"]["files"]["node_modules"]["files"]
        def locate(name):
            node = {"files": modules}
            for part in name.split("/"):
                node = node["files"][part]
            return node
        def blob(node, path):
            if node.get("unpacked"):
                return Path(str(asar)+".unpacked").joinpath("dsh/node_modules", path).read_bytes()
            stream.seek(base+int(node.get("offset", 0)))
            return stream.read(node["size"])
        def extract(node, path, imports):
            if "files" in node:
                for name, value in node["files"].items():
                    extract(value, path+"/"+name, imports)
            elif "size" in node:
                relative = Path(path)
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("ASAR 模块路径越过隔离目录")
                raw = blob(node, path)
                target = destination/"node_modules"/path
                if not target.resolve().is_relative_to(destination.resolve()):
                    raise ValueError("ASAR 提取目标越过隔离目录")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
                if path.endswith((".js", ".mjs")):
                    for name in re.findall(r'''(?:from\s*|import\s*\(?)["']([^"']+)["']''', raw.decode("utf-8", errors="ignore")):
                        if name.startswith((".", "/", "node:")):
                            continue
                        imports.add("/".join(name.split("/")[:2]) if name.startswith("@") else name.split("/")[0])
        done, queue = set(), list(seeds)
        while queue:
            name = queue.pop()
            if name in done:
                continue
            done.add(name)
            try:
                node = locate(name)
            except KeyError:
                continue  # Node 内置模块或未启用平台上的可选依赖。
            package = json.loads(blob(node["files"]["package.json"], name+"/package.json"))
            versions[name] = package.get("version")
            imports = set()
            extract(node, name, imports)
            queue.extend(set(package.get("dependencies", {})) | set(package.get("optionalDependencies", {})) | imports)
    (destination/"package.json").write_text('{"type":"module","private":true}', encoding="utf-8")
    return versions


def run(asar, output):
    root = Path(__file__).resolve().parents[1]
    node = shutil.which("node")
    if not node or not asar.is_file():
        raise ValueError("需要 Node.js 与已安装 Harness 的 app.asar 路径")
    with tempfile.TemporaryDirectory(prefix="memorydreaming-installed-host-") as folder:
        workspace = Path(folder)
        versions = extract_runtime(asar, workspace)
        driver = workspace/"acceptance.mjs"
        shutil.copy2(root/"scripts"/"harness_acceptance.mjs", driver)
        checks = []
        for mode in ("run-and-exit", "resume"):
            result = subprocess.run([node, str(driver), str(root), sys.executable, str(workspace), mode],
                                    cwd=workspace, capture_output=True, text=True, encoding="utf-8", timeout=60)
            if result.returncode:
                raise RuntimeError(result.stderr or result.stdout)
            report = json.loads(result.stdout.strip().splitlines()[-1])
            checks.extend(report["checked"])
        report["checked"] = checks
        report.update(versions=versions, application_archive_sha256=hashlib.sha256(asar.read_bytes()).hexdigest(),
                      real_desktop_ui=False, live_model=False,
                      note="Installed Cordis/agent/session/persistence/compaction code with deterministic provider; no user profile or credentials")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--asar", required=True)
    parser.add_argument("--output", default="dist/installed-harness-acceptance.json")
    args = parser.parse_args()
    print(json.dumps(run(Path(args.asar).resolve(), Path(args.output).resolve()), ensure_ascii=False, indent=2))
