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
    # disk 2000: base (key 0) -> delta after root (key 1) -> live delta after child (key 5)
    layout = NS(
        file=[NS(key=0, size=40 * GB), NS(key=1, size=5 * GB), NS(key=2, size=2 * GB), NS(key=3, size=1 * GB),
              NS(key=4, size=3 * GB), NS(key=5, size=4 * GB)],
        snapshot=[
            NS(key=snap_a, dataKey=2, memoryKey=-1, disk=[NS(key=2000, chain=[NS(fileKey=[0])])]),
            NS(key=snap_b, dataKey=3, memoryKey=4, disk=[NS(key=2000, chain=[NS(fileKey=[0]), NS(fileKey=[1])])]),
        ],
        disk=[NS(key=2000, chain=[NS(fileKey=[0]), NS(fileKey=[1]), NS(fileKey=[5])])],
    )
    folder = NS(name="Prod", parent=NS(name="vm", parent=NS(name="DC1", parent=None)))
    return NS(name="web-01", _moId="vm-101", parent=folder, snapshot=NS(rootSnapshotList=[root], currentSnapshot=snap_b),
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
        self.assertEqual(root.size_gb, 7.0)       # data 2 GB + delta written after it 5 GB; base disk excluded
        self.assertEqual(child.size_gb, 8.0)      # data 1 + mem 3 + live delta 4 (current snapshot)
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
        self.assertEqual(len(vcreport.filter_rows(rows, None, 7.5, None, None)), 1)
        self.assertEqual(len(vcreport.filter_rows(rows, None, 0, "^pre", None)), 1)
        self.assertEqual(len(vcreport.filter_rows(rows, None, 0, None, "db")), 0)

    def test_renderers(self):
        rows = vcreport.rows_for_vm(fake_vm(), NOW)
        table = vcreport.render_table(rows)
        self.assertIn("web-01", table)
        self.assertIn("2 snapshot(s), 15.00 GB total", table)
        csv_out = vcreport.render_csv(rows)
        self.assertEqual(csv_out.splitlines()[0].split(",")[0], "vm")
        self.assertEqual(len(csv_out.splitlines()), 3)


class RealWorldTests(unittest.TestCase):
    """Regressions found against real vCenter 7/8 inventories (sanitised fakes)."""

    def test_base_disk_not_counted(self):
        vm = fake_vm()
        # a single snapshot whose chain is only the 40 GB base must not report 40 GB
        vm.snapshot.rootSnapshotList[0].childSnapshotList = []
        vm.snapshot.currentSnapshot = vm.snapshot.rootSnapshotList[0].snapshot
        vm.layoutEx.disk = [NS(key=2000, chain=[NS(fileKey=[0]), NS(fileKey=[1])])]
        self.assertEqual(vcreport.rows_for_vm(vm, NOW)[0].size_gb, 7.0)

    def test_non_current_leaf_has_only_state_files(self):
        vm = fake_vm()
        vm.snapshot.currentSnapshot = None
        self.assertEqual(vcreport.rows_for_vm(vm, NOW)[1].size_gb, 4.0)  # vmsn 1 + vmem 3

    def test_disk_added_after_snapshot_ignored(self):
        vm = fake_vm()
        vm.layoutEx.file.append(NS(key=9, size=100 * GB))
        vm.layoutEx.disk.append(NS(key=2001, chain=[NS(fileKey=[9])]))
        self.assertEqual(vcreport.rows_for_vm(vm, NOW)[1].size_gb, 8.0)

    def test_refs_matched_by_moid_not_identity(self):
        # PropertyCollector returns distinct proxy objects for the same snapshot
        vm = fake_vm()
        a1, a2 = NS(_moId="snapshot-1"), NS(_moId="snapshot-1")
        vm.snapshot.rootSnapshotList[0].snapshot = a1
        vm.layoutEx.snapshot[0].key = a2
        self.assertEqual(vcreport.rows_for_vm(vm, NOW)[0].size_gb, 7.0)

    def test_missing_create_time(self):
        vm = fake_vm()
        vm.snapshot.rootSnapshotList[0].createTime = None
        row = vcreport.rows_for_vm(vm, NOW)[0]
        self.assertEqual((row.created, row.age_days), ("", 0.0))

    def test_vm_state(self):
        mk = lambda **kw: NS(runtime=NS(powerState=kw.get("p"), connectionState=kw.get("c", "connected")),  # noqa: E731
                             config=NS(template=kw.get("t", False)))
        self.assertEqual(vcreport.vm_state(mk(p="poweredOn")), "on")
        self.assertEqual(vcreport.vm_state(mk(p="suspended")), "suspended")
        self.assertEqual(vcreport.vm_state(mk(p="poweredOff", t=True)), "template")
        self.assertEqual(vcreport.vm_state(mk(p=None, c="inaccessible")), "inaccessible")
        self.assertEqual(vcreport.vm_state(NS(runtime=None, config=None)), "unknown")

    def test_records_from_properties(self):
        folders = [("group-v1", {"name": "vm", "parent": "datacenter-1"}),
                   ("datacenter-1", {"name": "DC1", "parent": "group-d1"}),
                   ("group-d1", {"name": "Datacenters", "parent": None}),
                   ("group-v2", {"name": "Prod", "parent": NS(_moId="group-v1")})]
        vms = [
            ("vm-1", {"name": "web-01", "parent": NS(_moId="group-v2"), "runtime.powerState": "poweredOn",
                      "layoutEx.file": [], "layoutEx.snapshot": [], "config.template": False}),
            ("vm-2", {"runtime.connectionState": "inaccessible"}),  # inaccessible: almost nothing returned
            ("vm-3", {"name": "tpl", "parent": NS(_moId="group-v1"), "config.template": True}),
        ]
        recs = vcreport.records_from_properties(vms, folders)
        self.assertEqual(recs[0].folder_path, "/DC1/Prod")
        self.assertEqual(vcreport.vm_state(recs[0]), "on")
        self.assertEqual((recs[1].name, vcreport.vm_state(recs[1]), recs[1].layoutEx), ("vm-2", "inaccessible", None))
        self.assertEqual(vcreport.rows_for_vm(recs[1], NOW), [])
        self.assertEqual(vcreport.vm_state(recs[2]), "template")
        self.assertEqual(recs[2].folder_path, "/DC1")

    def test_folder_path_cycle_guard(self):
        recs = vcreport.records_from_properties([("vm-1", {"name": "x", "parent": "f1"})],
                                                [("f1", {"name": "a", "parent": "f2"}), ("f2", {"name": "b", "parent": "f1"})])
        self.assertTrue(recs[0].folder_path.startswith("/"))


class CliTests(unittest.TestCase):
    def test_bad_duration_exit_1(self):
        self.assertEqual(vcreport.main(["--host", "h", "--user", "u", "--older-than", "x"]), 1)

    def test_missing_host_exit_1(self):
        self.assertEqual(vcreport.main(["--older-than", "7d"]), 1)

    def test_password_file_mode_warning(self):
        import contextlib
        import io
        import os
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            pf = os.path.join(d, "pw")
            with open(pf, "w", encoding="utf-8") as fh:
                fh.write("s3cret" + chr(10))
            os.chmod(pf, 0o644)
            args = vcreport.build_parser().parse_args(["--password-file", pf])
            for osname, warned in (("posix", True), ("nt", False)):
                err = io.StringIO()
                with mock.patch.object(vcreport.os, "name", osname), contextlib.redirect_stderr(err):
                    self.assertEqual(vcreport.read_password(args), "s3cret")
                self.assertEqual("chmod 0600" in err.getvalue(), warned, osname)

    def test_ca_file_flag_parses(self):
        args = vcreport.build_parser().parse_args(["--ca-file", "/tmp/ca.pem"])
        self.assertEqual(args.ca_file, "/tmp/ca.pem")


if __name__ == "__main__":
    unittest.main()
