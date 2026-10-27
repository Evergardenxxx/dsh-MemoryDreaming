"""本地编码进程的私有 JSONL 通道，无网络下载和模型工具。"""

import contextlib
import json
import sys
import os
import threading


def parent_lifetime():
    """宿主 bridge 被强制结束时，本项目创建的编码 worker 跟随退出。"""
    if sys.platform.startswith("linux"):
        import ctypes
        import signal
        parent = os.getppid()
        ctypes.CDLL(None).prctl(1, signal.SIGKILL)
        if os.getppid() != parent:
            os._exit(2)
    elif sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        handle = kernel.OpenProcess(0x00100000, False, os.getppid())
        if not handle:
            return
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        def watch():
            if kernel.WaitForSingleObject(handle, 0xFFFFFFFF) == 0:
                os._exit(2)
        threading.Thread(target=watch, name="memory-parent-watch", daemon=True).start()


def main():
    parent_lifetime()
    from .retrieval.embedding import SentenceTransformerEmbedder
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    output = sys.stdout
    model = None
    for line in sys.stdin:
        if len(line) > 8 * 1024 * 1024:
            return 2
        try:
            request = json.loads(line)
            with contextlib.redirect_stdout(sys.stderr):
                if model is None:
                    model = SentenceTransformerEmbedder(**request["config"])
                vectors = model._encode(request["texts"], request["prefix"])
            result = {"ok": True, "vectors": vectors}
        except Exception:
            result = {"ok": False, "code": "EMBEDDING_UNAVAILABLE"}
        output.write(json.dumps(result, separators=(",", ":"), allow_nan=False)+"\n")
        output.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
