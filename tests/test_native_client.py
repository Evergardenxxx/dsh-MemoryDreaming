"""Native client packaging and lazy module loading must work without a browser."""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class NativeClientTest(unittest.TestCase):
    def test_page_connection_is_lazy_and_cancellation_does_not_reset_conversation_bridge(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for native bridge isolation")
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="memorydreaming-ui-isolation-") as workspace:
            result = subprocess.run([node, str(root / "tests/fixtures/desktop-isolation.mjs"), workspace],
                                    cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_shipped_client_matches_sources_and_loads_in_native_factory(self):
        root = Path(__file__).resolve().parents[1]
        subprocess.run([sys.executable, str(root / "scripts/build_native_client.py"), "--check"],
                       cwd=root, capture_output=True, text=True, check=True)
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for native module loading")
        result = subprocess.run([node, str(root / "tests/fixtures/native-client.mjs"), str(root)],
                                cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
