"""跨平台本地安装：隔离 venv、原生插件副本和显式生成的绑定配置。"""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import venv


def install(destination, *, wheel=None, user="u1", character="c1", relationship="r1", domain="real"):
    repo = Path(__file__).resolve().parents[1]
    target = Path(destination).expanduser().resolve()
    if target == repo or repo in target.parents or target in repo.parents:
        raise ValueError("安装目录须位于源码目录之外，避免覆盖源码或用户数据")
    config = target / "plugin" / "cordis.patch.yml"
    if config.exists():
        raise ValueError("已存在安装配置，拒绝覆盖；请使用新的安装目录")
    if target.exists() and any(target.iterdir()):
        raise ValueError("安装目录必须为空，避免覆盖既有环境")
    bundled = sorted((repo/"wheels").glob("memorydreaming-*.whl"))
    wheel = wheel or (str(bundled[-1]) if bundled else None)
    package = Path(wheel).resolve() if wheel else repo
    if wheel and (not package.is_file() or package.suffix != ".whl"):
        raise ValueError("wheel 必须指向现有 .whl 文件")
    target.mkdir(parents=True, exist_ok=True)
    environment = target / "venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    args = [str(python), "-m", "pip", "install", "--no-deps"]
    if wheel:
        args.append("--no-index")
    subprocess.run([*args, str(package)], check=True)
    plugin = target / "plugin"
    plugin.mkdir(exist_ok=True)
    source = repo / "integrations" / "deepseek-harness"
    for name in ("lib", "locale", "desktop"):
        shutil.copytree(source/name, plugin/name, dirs_exist_ok=True)
    for name in ("package.json", "icon.svg", "README.md", "LICENSE"):
        shutil.copy2(source/name, plugin/name)
    settings = {"enabled": True, "pythonCommand": str(python), "repoPath": str(target),
                "dbPath": str(target/"data"/"memory.db"), "userId": user, "characterId": character,
                "relationshipId": relationship, "domain": domain, "maxTokens": 1200,
                "bridgeTimeoutMs": 15000, "autoPrefetch": True, "activationMode": "command"}
    config.write_text(json.dumps([{"insert": [{"id": "memorydreaming-plugin", "name": "@local/memorydreaming",
                                               "config": settings}]}], ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    subprocess.run([str(python), "-X", "utf8", "-m", "memorydreaming", "--db", str(target/"data"/"memory.db"), "init"], check=True)
    return {"plugin_path": str(plugin), "python": str(python), "database": settings["dbPath"],
            "next_action": "Install plugin_path in the Harness native plugin manager; no application settings were modified."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Install MemoryDreaming into a new local directory")
    parser.add_argument("--destination", required=True)
    parser.add_argument("--wheel")
    parser.add_argument("--user", default="u1")
    parser.add_argument("--character", default="c1")
    parser.add_argument("--relationship", default="r1")
    parser.add_argument("--domain", default="real")
    args = parser.parse_args()
    print(json.dumps(install(args.destination, wheel=args.wheel, user=args.user, character=args.character,
                             relationship=args.relationship, domain=args.domain), ensure_ascii=False, indent=2))
