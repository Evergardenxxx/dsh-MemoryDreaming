"""Native desktop authorization, command admission and cancellation regressions.

These tests use temporary state and a deterministic host boundary. The separate
installed-runtime driver verifies actual Cordis/Gateway invocation tracing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "fixtures" / "desktop-controller.mjs"


@unittest.skipUnless(shutil.which("node"), "Node.js is required for native desktop regressions")
class DesktopControllerTest(unittest.TestCase):
    def run_scenario(self, scenario):
        with tempfile.TemporaryDirectory(prefix="memorydreaming-desktop-test-") as folder:
            result = subprocess.run(
                [shutil.which("node"), str(DRIVER), scenario, folder],
                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=20,
            )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["scenario"], scenario)
        self.assertFalse(report["live_model"])

    def test_owner_identity_comes_from_invocation_not_page_arguments(self):
        self.run_scenario("auth")

    def test_window_snapshot_is_bounded_excludes_subagents_and_keeps_bridge_idle(self):
        self.run_scenario("windows")

    def test_management_calls_keep_the_bridge_envelope_and_report_source_errors(self):
        self.run_scenario("bridge")

    def test_bound_role_management_without_live_windows_never_enables_capture(self):
        self.run_scenario("no_windows")

    def test_unknown_actions_identity_fields_and_malformed_control_requests_are_refused(self):
        self.run_scenario("invalid")

    def test_commands_are_cas_guarded_idempotent_and_report_admission_only(self):
        self.run_scenario("controls")

    def test_each_button_submits_one_complete_fixed_native_user_command(self):
        self.run_scenario("command_modes")

    def test_busy_unavailable_and_failed_admission_do_not_leave_false_receipts(self):
        self.run_scenario("control_errors")

    def test_cancel_only_stops_a_running_sleep_command_and_is_idempotent(self):
        self.run_scenario("cancel")

    def test_progress_turn_and_unload_lifecycles_release_owned_state(self):
        self.run_scenario("lifecycle")

    def test_owner_abort_and_plugin_unload_abort_pending_management_reads(self):
        self.run_scenario("abort")

    def test_aborted_prompt_admission_releases_the_window_queue(self):
        self.run_scenario("prompt_abort")


if __name__ == "__main__":
    unittest.main()
