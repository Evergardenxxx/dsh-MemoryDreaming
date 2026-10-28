"""构建本地开源发布目录与校验清单，不推送、打 tag 或发布。"""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile
import tempfile


def npm_command():
    npm = shutil.which("npm")
    if npm is None:
        raise RuntimeError("Node.js/npm are required to build the native plugin")
    if sys.platform == "win32":
        node = shutil.which("node")
        cli = Path(node).parent/"node_modules"/"npm"/"bin"/"npm-cli.js"
        if not cli.is_file():
            raise RuntimeError("Cannot locate npm-cli.js next to Node.js")
        return [node, str(cli)]
    return [npm]


def build(destination):
    root = Path(__file__).resolve().parents[1]
    output = Path(destination).resolve()
    if subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root, capture_output=True, text=True, check=True).stdout:
        raise RuntimeError("Commit tracked changes before creating the final release bundle")
    subprocess.run([sys.executable, str(root/"scripts/build_native_client.py"), "--check"], cwd=root, check=True)
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(output), str(root)], check=True)
    native = root/"integrations"/"deepseek-harness"
    pack = json.loads(subprocess.run([*npm_command(), "pack", "--json", "--pack-destination", str(output)],
                                    cwd=native, text=True, capture_output=True, check=True).stdout)[0]
    package_version = json.loads(subprocess.run([sys.executable, "-c", "import json,memorydreaming; print(json.dumps(memorydreaming.__version__))"],
        cwd=root, capture_output=True, text=True, check=True).stdout)
    wheel = output/f"memorydreaming-{package_version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as archive:
        if not {"memorydreaming/storage/schema.sql", "memorydreaming/desktop.py"}.issubset(archive.namelist()) or not any(name.endswith("LICENSE") for name in archive.namelist()):
            raise RuntimeError("Wheel missing schema, desktop service or license")
    packed_files = {entry["path"] for entry in pack["files"]}
    if not {"lib/client.js", "lib/desktop-controller.js", "desktop/view.js", "desktop/view.css"}.issubset(packed_files):
        raise RuntimeError("Native plugin missing desktop client or source files")
    bundle = output/f"memorydreaming-{package_version}-offline.zip"
    with tempfile.TemporaryDirectory(prefix="memorydreaming-release-") as folder:
        source = Path(folder)/"source.zip"
        subprocess.run(["git", "archive", "--format=zip", "-o", str(source), "HEAD"], cwd=root, check=True)
        with zipfile.ZipFile(source) as source_archive, zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as target:
            for name in source_archive.namelist():
                target.writestr(name, source_archive.read(name))
            target.write(wheel, "wheels/"+wheel.name)
            target.write(output/pack["filename"], "bundles/"+pack["filename"])
    # 发布脚本必须从已提交版本运行，避免校验清单和源码包不同步。
    if subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root, capture_output=True, text=True, check=True).stdout:
        raise RuntimeError("Commit tracked changes before creating the final release bundle")
    artifacts = [wheel, output/pack["filename"], bundle]
    checksums = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in artifacts}
    (output/"SHA256SUMS").write_text("".join(f"{value}  {name}\n" for name, value in checksums.items()), encoding="utf-8")
    (output/"release-manifest.json").write_text(json.dumps({"version": package_version,
        "native_version": pack["version"], "artifacts": checksums}, indent=2)+"\n", encoding="utf-8")
    return checksums


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="dist/release")
    args = parser.parse_args()
    print(json.dumps(build(args.output), indent=2))
