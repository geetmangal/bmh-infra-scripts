"""Unit tests for vmware/vcenter-snapshot-report.py pure logic (no pyvmomi needed)."""
import importlib.util
import pathlib
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS  # noqa: N814

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("vcreport", ROOT / "vmware" / "vcenter-snapshot-report.py")
vcreport = importlib.util.module_from_spec(SPEC)
sys.modules["vcreport"] = vcreport
SPEC.loader.exec_module(vcreport)  # type: ignore[union-attr]

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
GB = 1024 ** 3


def fake_vm():
    snap_a, snap_b = object(), object()
    child = NS(name="child", id=2, snapshot=snap_b, createTime=NOW - timedelta(hours=36), description="", quiesced=True, childSnapshotList=[])
    root = NS(name="pre-upgrade", id=1, snapshot=snap_a, createTime=NOW - timedelta(days=10), description="before\nupgrade", quiesced=False, childSnapshotList=[child])
    layout = NS(
        file=[NS(key=0, size=40 * GB), NS(key=1, size=5 * GB), NS(key=2, size=2 * GB), NS(key=3, size=1 * GB), NS(key=4, size=3 * GB)],
        snapshot=[
            NS(key=snap_a, dataKey=2, memoryKey=-1, disk=[NS(chain=[NS(fileKey=[1])])]),
            NS(key=snap_b, dataKey=3, memoryKey=4, disk=[NS(chain=[NS(fileKey=[1])])]),  # shares key 1, counted once
        ],
    )
    folder = NS(name="Prod", parent=NS(name="vm", parent=NS(name="DC1", parent=None)))
    return NS(name="web-01", _moId="vm-101", parent=folder, snapshot=NS(rootSnapshotList=[root]),
              layoutEx=layout, runtime=NS(powerState="poweredOn", consolidationNeeded=True))


class DurationTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(vcreport.parse_duration("7d"), timedelta(days=7))
        self.assertEqual(vcreport.parse_duration("36h"), timedelta(hours=36))
        self.assertEqual(vcreport.parse_duration("2w"), timedelta(weeks=2))
        self.assertEqual(vcreport.parse_duration("3"), timedelta(days=3))
        with self.assertRaises(ValueError):
            vcreport.parse_duration("soon")

    def test_age_days(self):
        self.assertEqual(vcreport.age_days(NOW - timedelta(days=2, hours=12), NOW), 2.5)
        self.assertEqual(vcreport.age_days((NOW - timedelta(days=1)).replace(tzinfo=None), NOW), 1.0)


class RowTests(unittest.TestCase):
    def test_rows_and_sizes(self):
        rows = vcreport.rows_for_vm(fake_vm(), NOW, tags="owner=platform")
        self.assertEqual([r.snapshot for r in rows], ["pre-upgrade", "child"])
        root, child = rows
        self.assertEqual(root.size_gb, 7.0)       # data 2 GB + disk 5 GB
        self.assertEqual(child.size_gb, 9.0)      # data 1 + mem 3 + disk 5
        self.assertEqual(root.age_days, 10.0)
        self.assertEqual(child.age_days, 1.5)
        self.assertEqual(root.children, 1)
        self.assertEqual(root.power_state, "on")
        self.assertEqual(root.folder, "/DC1/Prod")
        self.assertEqual(root.description, "before upgrade")
        self.assertTrue(root.consolidate_needed)
        self.assertTrue(child.quiesced)
        self.assertEqual(root.created, "2026-10-15T12:00:00Z")

    def test_no_snapshots(self):
        vm = fake_vm()
        vm.snapshot = None
        self.assertEqual(vcreport.rows_for_vm(vm, NOW), [])

    def test_missing_layout(self):
        vm = fake_vm()
        vm.layoutEx = None
        self.assertEqual(vcreport.rows_for_vm(vm, NOW)[0].size_gb, 0.0)

    def test_filters(self):
        rows = vcreport.rows_for_vm(fake_vm(), NOW)
        self.assertEqual(len(vcreport.filter_rows(rows, timedelta(days=7), 0, None, None)), 1)
        self.assertEqual(len(vcreport.filter_rows(rows, None, 8, None, None)), 1)
        self.assertEqual(len(vcreport.filter_rows(rows, None, 0, "^pre", None)), 1)
        self.assertEqual(len(vcreport.filter_rows(rows, None, 0, None, "db")), 0)

    def test_renderers(self):
        rows = vcreport.rows_for_vm(fake_vm(), NOW)
        table = vcreport.render_table(rows)
        self.assertIn("web-01", table)
        self.assertIn("2 snapshot(s), 16.00 GB total", table)
        csv_out = vcreport.render_csv(rows)
        self.assertEqual(csv_out.splitlines()[0].split(",")[0], "vm")
        self.assertEqual(len(csv_out.splitlines()), 3)


class CliTests(unittest.TestCase):
    def test_bad_duration_exit_1(self):
        self.assertEqual(vcreport.main(["--host", "h", "--user", "u", "--older-than", "x"]), 1)

    def test_missing_host_exit_1(self):
        self.assertEqual(vcreport.main(["--older-than", "7d"]), 1)


if __name__ == "__main__":
    unittest.main()
