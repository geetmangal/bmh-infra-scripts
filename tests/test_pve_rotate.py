"""Tests for proxmox/pve-snapshot-rotate.sh using a stub curl that fakes the PVE API.

Regressions: in --api mode the task UPID was never polled, so a snapshot task that
failed later was reported as ok; --keep 0 silently behaved like --keep 1.
"""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "proxmox" / "pve-snapshot-rotate.sh"

# Fake PVE API. FAKE_TASK_STATUS: "stopped" or "running"; FAKE_EXIT: task exitstatus.
CURL = r'''#!/bin/bash
method=GET; url=""
while [[ $# -gt 0 ]]; do case "$1" in -X) method="$2"; shift 2;; -H|--max-time|--data-urlencode) shift 2;; -*) shift;; *) url="$1"; shift;; esac; done
path="${url#*/api2/json}"
echo "$method $path" >> "$STUB_LOG"
case "$method $path" in
  "GET /cluster/resources"*) echo '{"data":[{"vmid":100,"type":"qemu","node":"pve1","name":"guest","status":"running","tags":""}]}' ;;
  "GET /nodes/pve1/qemu/100/config") echo '{"data":{}}' ;;
  "GET /nodes/pve1/qemu/100/snapshot") echo '{"data":[{"name":"current"}]}' ;;
  "POST /nodes/pve1/qemu/100/snapshot") echo '{"data":"UPID:pve1:00001234:00ABCDEF:65000000:qmsnapshot:100:root@pam:"}' ;;
  "GET /nodes/pve1/tasks/"*"/status") printf '{"data":{"status":"%s","exitstatus":"%s"}}\n' "$FAKE_TASK_STATUS" "$FAKE_EXIT" ;;
  *) echo '{"data":null}'; exit 22 ;;
esac
'''


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "needs bash + jq")
class PveRotateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        (self.tmp / "bin").mkdir()
        curl = self.tmp / "bin" / "curl"
        curl.write_text(CURL)
        curl.chmod(0o755)
        self.log = self.tmp / "api.log"
        self.env = dict(os.environ, PATH=f"{self.tmp / 'bin'}:{os.environ['PATH']}", STUB_LOG=str(self.log),
                        PVE_API_HOST="https://pve.invalid:8006", PVE_API_TOKEN="test@pve!ci=00000000-0000-0000-0000-000000000000",
                        PVE_TASK_POLL="0.1", NO_COLOR="1", FAKE_TASK_STATUS="stopped", FAKE_EXIT="OK")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_script(self, *extra, **env):
        e = dict(self.env, **env)
        return subprocess.run(["bash", str(SCRIPT), "--api", "--node", "pve1", "--vmids", "100", "--json", *extra],
                              check=False, env=e, capture_output=True, text=True, timeout=60)

    def summary(self, r):
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_task_ok(self):
        r = self.run_script("--keep", "3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.summary(r)["ok"], 1)
        self.assertIn("/status", self.log.read_text())

    def test_task_failing_later_is_reported(self):
        r = self.run_script("--keep", "3", FAKE_EXIT="snapshot feature is not available")
        self.assertEqual(r.returncode, 3, r.stderr)
        s = self.summary(r)
        self.assertEqual((s["ok"], s["failed"]), (0, 1))
        self.assertEqual(s["guests"][0]["error"], "create")
        self.assertIn("snapshot feature is not available", r.stderr)

    def test_task_timeout(self):
        r = self.run_script("--keep", "3", "--timeout", "1", FAKE_TASK_STATUS="running")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("after 1s", r.stderr)

    def test_keep_zero_rejected(self):
        r = self.run_script("--keep", "0")
        self.assertEqual(r.returncode, 1)
        self.assertIn("--keep must be >= 1", r.stderr)
        self.assertFalse(self.log.exists(), "no API call may happen with an invalid --keep")


if __name__ == "__main__":
    unittest.main()
