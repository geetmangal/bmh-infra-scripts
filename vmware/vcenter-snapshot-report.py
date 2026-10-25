#!/usr/bin/env python3
"""vcenter-snapshot-report.py - report on (and optionally delete) VMware vCenter snapshots.

Part of bmh-infra-scripts (https://github.com/geetmangal/bmh-infra-scripts)
Maintained by the BookMyHost engineering team - https://bookmyhost.com
Licence: MIT

Requires pyvmomi (``pip install -r requirements.txt``).  The import is guarded so
``--help`` and the unit tests work without it.

Credentials: env VC_HOST, VC_USER, VC_PASS  -or-  --password-file (mode 0600).

Exit codes
----------
0  success
1  usage / argument error
2  pyvmomi missing or connection failure
3  one or more snapshot deletions failed
4  --delete requested without --confirm (nothing changed)
"""
from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import re
import ssl
import stat
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

__version__ = "1.0.0"

try:  # pragma: no cover - exercised only with pyvmomi installed
    from pyVim.connect import Disconnect, SmartConnect
    from pyVmomi import vim, vmodl

    HAVE_PYVMOMI = True
except ImportError:  # pragma: no cover
    HAVE_PYVMOMI = False
    vim = vmodl = None  # type: ignore[assignment]


def colour(code: str, text: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"\033[{code}m{text}\033[0m"


def log(msg: str, level: str = "INFO") -> None:
    codes = {"INFO": "34", "OK": "32", "WARN": "33", "ERROR": "31"}
    if sys.stderr.isatty() and not os.environ.get("NO_COLOR"):
        msg = f"\033[{codes.get(level, '0')}m[{level}] {msg}\033[0m"
    else:
        msg = f"[{level}] {msg}"
    print(msg, file=sys.stderr)


# --------------------------------------------------------------------------- pure logic
@dataclass
class SnapshotRow:
    vm: str
    vm_moid: str
    power_state: str
    folder: str
    tags: str
    snapshot: str
    snapshot_id: int
    created: str            # ISO-8601 UTC
    age_days: float
    size_gb: float
    description: str
    consolidate_needed: bool
    quiesced: bool
    children: int


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhdw]?)\s*$", re.I)
_UNITS = {"": 86400, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> timedelta:
    """'7d' -> 7 days, '36h', '2w', bare number = days. Raises ValueError."""
    m = _DURATION_RE.match(str(text))
    if not m:
        raise ValueError(f"invalid duration {text!r} (use e.g. 7d, 36h, 2w)")
    return timedelta(seconds=int(m.group(1)) * _UNITS[m.group(2).lower()])


def age_days(created: datetime, now: Optional[datetime] = None) -> float:
    now = now or datetime.now(timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return round((now - created).total_seconds() / 86400.0, 2)


def _ref_id(ref: Any) -> Any:
    """Stable identity for a snapshot ref: the MoRef id on real pyvmomi objects."""
    moid = getattr(ref, "_moId", None)
    return moid if moid is not None else id(ref)


def _chain_keys(disks: Any) -> Dict[Any, set]:
    """{diskKey: set(fileKeys)} for a layoutEx disk list (snapshot[].disk or disk)."""
    out: Dict[Any, set] = {}
    for disk in disks or []:
        keys: set = set()
        for chain in getattr(disk, "chain", None) or []:
            keys.update(getattr(chain, "fileKey", None) or [])
        out[getattr(disk, "key", None)] = keys
    return out


def snapshot_size_bytes(layout_ex: Any, snapshot_ref: Any, children: Iterable[Any] = (),
                        is_current: bool = False) -> int:
    """Bytes a snapshot holds on the datastore, from VM layoutEx.

    layoutEx.snapshot[].disk[].chain is the *full* disk chain at the time of the
    snapshot (base disk included), so summing it counts the base disk too.  The space
    attributable to snapshot S is its state/memory files plus the delta disks written
    after S: files in a child snapshot's chain (or, for the current snapshot, the
    VM's live disk chain) that are not already in S's chain.
    """
    if layout_ex is None or not getattr(layout_ex, "file", None):
        return 0
    size_by_key: Dict[int, int] = {f.key: int(getattr(f, "size", 0) or 0) for f in layout_ex.file}
    by_ref = {_ref_id(s.key): s for s in getattr(layout_ex, "snapshot", None) or []}
    snap = by_ref.get(_ref_id(snapshot_ref))
    if snap is None:
        return 0
    keys: set = set()
    for attr in ("dataKey", "memoryKey"):
        val = getattr(snap, attr, None)
        if val is not None and val >= 0:
            keys.add(val)
    own = _chain_keys(getattr(snap, "disk", None))
    nexts = [_chain_keys(getattr(by_ref[_ref_id(c)], "disk", None)) for c in children if _ref_id(c) in by_ref]
    if is_current:
        nexts.append(_chain_keys(getattr(layout_ex, "disk", None)))
    for nxt in nexts:
        for disk_key, files in nxt.items():
            if disk_key in own:  # a disk added after the snapshot is not snapshot data
                keys.update(files - own[disk_key])
    return sum(size_by_key.get(k, 0) for k in keys)


def flatten_snapshot_tree(tree: Iterable[Any]) -> List[Any]:
    """Depth-first flatten of VirtualMachineSnapshotTree[] into a list of tree nodes."""
    out: List[Any] = []
    for node in tree or []:
        out.append(node)
        out.extend(flatten_snapshot_tree(getattr(node, "childSnapshotList", None) or []))
    return out


def folder_path(vm: Any) -> str:
    parts: List[str] = []
    parent = getattr(vm, "parent", None)
    depth = 0
    while parent is not None and depth < 32:
        name = getattr(parent, "name", None)
        if name and name not in ("vm", "Datacenters"):
            parts.append(name)
        parent = getattr(parent, "parent", None)
        depth += 1
    return "/" + "/".join(reversed(parts))


def vm_state(vm: Any) -> str:
    """'on'/'off'/'suspended', 'template', or the connection state for inaccessible/orphaned VMs."""
    runtime = getattr(vm, "runtime", None)
    conn = str(getattr(runtime, "connectionState", "connected") or "connected")
    if conn != "connected":
        return conn
    if getattr(getattr(vm, "config", None), "template", False):
        return "template"
    power = str(getattr(runtime, "powerState", None) or "unknown")
    return {"poweredOn": "on", "poweredOff": "off"}.get(power, power)


def records_from_properties(vm_objs: Iterable[Any], folder_objs: Iterable[Any] = ()) -> List[Any]:
    """Turn PropertyCollector results into VM-like records that rows_for_vm() accepts.

    Each input item is ``(moid, {propPath: value})``.  Missing properties (inaccessible
    or orphaned VMs, permission gaps) simply stay ``None``.  ``folder_objs`` carries
    ``name``/``parent`` for Folder and Datacenter objects so the folder path is
    resolved locally instead of with one round trip per parent.
    """
    from types import SimpleNamespace as NS  # noqa: N814

    folders = {moid: props for moid, props in folder_objs}

    def path(parent: Any) -> str:
        parts: List[str] = []
        seen = 0
        pid = getattr(parent, "_moId", parent)
        while pid in folders and seen < 64:
            name = folders[pid].get("name")
            if name and name not in ("vm", "Datacenters"):
                parts.append(name)
            nxt = folders[pid].get("parent")
            pid = getattr(nxt, "_moId", nxt)
            seen += 1
        return "/" + "/".join(reversed(parts))

    out = []
    for moid, p in vm_objs:
        layout = None
        if p.get("layoutEx.file") is not None or p.get("layoutEx.snapshot") is not None:
            layout = NS(file=p.get("layoutEx.file") or [], snapshot=p.get("layoutEx.snapshot") or [],
                        disk=p.get("layoutEx.disk") or [])
        out.append(NS(
            _moId=str(moid),
            name=p.get("name") or str(moid),
            snapshot=p.get("snapshot"),
            layoutEx=layout,
            config=NS(template=bool(p.get("config.template"))),
            runtime=NS(powerState=p.get("runtime.powerState"),
                       connectionState=p.get("runtime.connectionState") or "connected",
                       consolidationNeeded=bool(p.get("runtime.consolidationNeeded"))),
            customValue=p.get("customValue") or [],
            folder_path=path(p.get("parent")),
        ))
    return out


def rows_for_vm(vm: Any, now: Optional[datetime] = None, tags: str = "") -> List[SnapshotRow]:
    """Build report rows for one VM-like object (real pyvmomi VM or test fake)."""
    snapinfo = getattr(vm, "snapshot", None)
    if snapinfo is None or not getattr(snapinfo, "rootSnapshotList", None):
        return []
    layout = getattr(vm, "layoutEx", None)
    runtime = getattr(vm, "runtime", None)
    power = vm_state(vm)
    consolidate = bool(getattr(runtime, "consolidationNeeded", False))
    moid = str(getattr(vm, "_moId", "") or "")
    current = getattr(snapinfo, "currentSnapshot", None)
    folder = getattr(vm, "folder_path", None) or folder_path(vm)
    rows = []
    for node in flatten_snapshot_tree(snapinfo.rootSnapshotList):
        created = getattr(node, "createTime", None)
        kids = getattr(node, "childSnapshotList", None) or []
        is_current = current is not None and _ref_id(current) == _ref_id(node.snapshot)
        size = snapshot_size_bytes(layout, node.snapshot, [k.snapshot for k in kids], is_current)
        rows.append(SnapshotRow(
            vm=vm.name,
            vm_moid=moid,
            power_state=power,
            folder=folder,
            tags=tags,
            snapshot=node.name or "",
            snapshot_id=int(node.id),
            created=(created.astimezone(timezone.utc) if created.tzinfo else created).strftime("%Y-%m-%dT%H:%M:%SZ") if created else "",
            age_days=age_days(created, now) if created else 0.0,
            size_gb=round(size / (1024 ** 3), 2),
            description=(node.description or "").strip().replace("\n", " ")[:120],
            consolidate_needed=consolidate,
            quiesced=bool(getattr(node, "quiesced", False)),
            children=len(getattr(node, "childSnapshotList", None) or []),
        ))
    return rows


def filter_rows(rows: List[SnapshotRow], older_than: Optional[timedelta], min_size_gb: float,
                name_regex: Optional[str], vm_regex: Optional[str]) -> List[SnapshotRow]:
    out = rows
    if older_than is not None:
        min_days = older_than.total_seconds() / 86400.0
        out = [r for r in out if r.age_days >= min_days]
    if min_size_gb > 0:
        out = [r for r in out if r.size_gb >= min_size_gb]
    if name_regex:
        rx = re.compile(name_regex)
        out = [r for r in out if rx.search(r.snapshot)]
    if vm_regex:
        rx = re.compile(vm_regex)
        out = [r for r in out if rx.search(r.vm)]
    return out


def render_table(rows: List[SnapshotRow]) -> str:
    cols = [("vm", 28), ("power_state", 5), ("snapshot", 30), ("age_days", 8), ("size_gb", 8), ("created", 20), ("folder", 24)]
    head = "  ".join(f"{c:<{w}}" for c, w in cols)
    lines = [colour("1", head), "-" * len(head)]
    for r in rows:
        d = asdict(r)
        cells = []
        for c, w in cols:
            v = str(d[c])
            if c == "age_days" and r.age_days >= 30:
                v = colour("31", f"{v:>{w}}")
            elif c == "age_days":
                v = f"{v:>{w}}"
            elif c == "size_gb":
                v = f"{v:>{w}}"
            else:
                v = f"{v[:w]:<{w}}"
            cells.append(v)
        lines.append("  ".join(cells))
    total = sum(r.size_gb for r in rows)
    lines.append("-" * len(head))
    lines.append(f"{len(rows)} snapshot(s), {total:.2f} GB total, {len({r.vm for r in rows})} VM(s)")
    return "\n".join(lines)


def render_csv(rows: List[SnapshotRow]) -> str:
    import io

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(SnapshotRow.__dataclass_fields__))  # type: ignore[attr-defined]
    w.writeheader()
    for r in rows:
        w.writerow(asdict(r))
    return buf.getvalue()


# --------------------------------------------------------------------------- vCenter I/O
def read_password(args: argparse.Namespace) -> str:
    if args.password_file:
        try:
            st = os.stat(args.password_file)
        except OSError as exc:
            raise SystemExit(f"[ERROR] cannot stat {args.password_file}: {exc}") from exc
        # POSIX mode bits are meaningless on Windows (NTFS always reports 0o666/0o777)
        if os.name != "nt" and st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            log(f"{args.password_file} is readable by group/others; chmod 0600 it", "WARN")
        with open(args.password_file, encoding="utf-8") as fh:
            return fh.readline().rstrip("\r\n")
    pw = os.environ.get("VC_PASS")
    if pw:
        return pw
    if sys.stdin.isatty():
        return getpass.getpass(f"Password for {args.user}@{args.host}: ")
    raise SystemExit("[ERROR] no password: set VC_PASS or use --password-file")


def connect(args: argparse.Namespace):
    if not HAVE_PYVMOMI:
        log("pyvmomi is not installed. Run: pip install -r requirements.txt  (or pip install pyvmomi)", "ERROR")
        sys.exit(2)
    try:
        ctx = ssl.create_default_context(cafile=args.ca_file) if args.ca_file else ssl.create_default_context()
    except (OSError, ssl.SSLError) as exc:
        log(f"cannot load --ca-file {args.ca_file}: {exc}", "ERROR")
        sys.exit(2)
    if args.insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        si = SmartConnect(host=args.host, user=args.user, pwd=read_password(args), port=args.port, sslContext=ctx)
    except ssl.SSLError as exc:
        log(f"TLS verification of {args.host} failed: {exc}. Use --ca-file <vCenter CA bundle> or --insecure", "ERROR")
        sys.exit(2)
    except Exception as exc:
        log(f"connection to {args.host} failed: {exc}", "ERROR")
        sys.exit(2)
    return si


VM_PROPS = ["name", "parent", "snapshot", "layoutEx.file", "layoutEx.snapshot", "layoutEx.disk",
            "config.template", "runtime.powerState", "runtime.connectionState",
            "runtime.consolidationNeeded"]


def collect(content: Any, obj_types: List[Any], paths: List[str], page: int = 500) -> List[Any]:
    """Fetch ``paths`` for every object of ``obj_types`` in one paged PropertyCollector
    call over a ContainerView, instead of one round trip per object and property."""
    view = content.viewManager.CreateContainerView(content.rootFolder, obj_types, True)
    try:
        trav = vmodl.query.PropertyCollector.TraversalSpec(name="view", path="view", skip=False, type=vim.view.ContainerView)
        spec = vmodl.query.PropertyCollector.FilterSpec(
            objectSet=[vmodl.query.PropertyCollector.ObjectSpec(obj=view, skip=True, selectSet=[trav])],
            propSet=[vmodl.query.PropertyCollector.PropertySpec(type=t, pathSet=paths, all=False) for t in obj_types],
            reportMissingObjectsInResults=False,
        )
        pc = content.propertyCollector
        out: List[Any] = []
        res = pc.RetrievePropertiesEx([spec], vmodl.query.PropertyCollector.RetrieveOptions(maxObjects=page))
        while res:
            for oc in res.objects:
                out.append((oc.obj._moId, {dp.name: dp.val for dp in (oc.propSet or [])}))
            res = pc.ContinueRetrievePropertiesEx(res.token) if res.token else None
        return out
    finally:
        view.Destroy()


def vm_tags(content: Any, records: List[Any]) -> Dict[str, str]:
    """Best-effort tag lookup via custom attributes (vSphere Tags need the REST API)."""
    out: Dict[str, str] = {}
    try:
        fields = {f.key: f.name for f in content.customFieldsManager.field}
    except Exception as exc:
        log(f"tag lookup skipped: {exc}", "WARN")
        return out
    for rec in records:
        vals = [f"{fields.get(cv.key, cv.key)}={cv.value}" for cv in (rec.customValue or []) if getattr(cv, "value", None)]
        if vals:
            out[rec._moId] = ";".join(vals)
    return out


def wait_task(task: Any) -> None:
    import time

    while task.info.state in (vim.TaskInfo.State.running, vim.TaskInfo.State.queued):
        time.sleep(1)
    if task.info.state != vim.TaskInfo.State.success:
        raise RuntimeError(str(task.info.error.msg if task.info.error else task.info.state))


def find_snapshot_obj(vm: Any, snapshot_id: int) -> Any:
    for node in flatten_snapshot_tree(vm.snapshot.rootSnapshotList if vm.snapshot else []):
        if int(node.id) == snapshot_id:
            return node.snapshot
    return None


# --------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vcenter-snapshot-report.py",
        description="List vCenter VM snapshots with age and size; filter, export, and optionally delete them safely.",
        epilog=(
            "Examples:\n"
            "  VC_HOST=vc.example.com VC_USER=svc@vsphere.local VC_PASS=... %(prog)s --older-than 7d\n"
            "  %(prog)s --older-than 14d --min-size-gb 20 --format csv > old-snaps.csv\n"
            "  %(prog)s --consolidate-needed --format json\n"
            "  %(prog)s --older-than 30d --delete --dry-run\n"
            "  %(prog)s --older-than 30d --delete --confirm --max-delete 10\n\n"
            "Exit codes: 0 ok, 1 usage, 2 connection/pyvmomi, 3 deletion failures, 4 --delete without --confirm"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--host", default=os.environ.get("VC_HOST"), help="vCenter host (env VC_HOST)")
    p.add_argument("--user", default=os.environ.get("VC_USER"), help="user (env VC_USER)")
    p.add_argument("--password-file", help="0600 file holding the password (else env VC_PASS or prompt)")
    p.add_argument("--port", type=int, default=443)
    p.add_argument("--insecure", action="store_true", help="skip TLS certificate verification (self-signed VMCA)")
    p.add_argument("--ca-file", help="PEM CA bundle to verify the vCenter certificate (e.g. the VMCA root)")
    p.add_argument("--older-than", help="only snapshots at least this old, e.g. 7d, 36h, 2w")
    p.add_argument("--min-size-gb", type=float, default=0.0, help="only snapshots >= this size (GB, from layoutEx)")
    p.add_argument("--name-regex", help="only snapshots whose name matches")
    p.add_argument("--vm-regex", help="only VMs whose name matches")
    p.add_argument("--consolidate-needed", action="store_true", help="report VMs flagged consolidationNeeded instead of snapshots")
    p.add_argument("--no-tags", action="store_true", help="skip the custom-attribute tag lookup (faster)")
    p.add_argument("--format", choices=["table", "csv", "json"], default="table")
    p.add_argument("--sort", choices=["age", "size", "vm"], default="age")
    p.add_argument("--delete", action="store_true", help="delete the matching snapshots (needs --confirm)")
    p.add_argument("--confirm", action="store_true", help="really delete (without it --delete only previews)")
    p.add_argument("--max-delete", type=int, default=20, help="safety cap on deletions per run (default 20)")
    p.add_argument("--dry-run", action="store_true", help="show what would be deleted, change nothing")
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        older = parse_duration(args.older_than) if args.older_than else None
    except ValueError as exc:
        log(str(exc), "ERROR")
        return 1
    if args.max_delete < 1:
        log("--max-delete must be >= 1", "ERROR")
        return 1
    if not args.host or not args.user:
        log("--host/--user (or VC_HOST/VC_USER) are required", "ERROR")
        return 1
    if args.delete and not args.confirm and not args.dry_run:
        log("--delete without --confirm: preview only; add --confirm to delete", "WARN")

    si = connect(args)
    try:
        import time

        t0 = time.monotonic()
        content = si.RetrieveContent()
        props = VM_PROPS + ([] if args.no_tags else ["customValue"])
        vms = records_from_properties(collect(content, [vim.VirtualMachine], props),
                                      collect(content, [vim.Folder, vim.Datacenter], ["name", "parent"]))
        tags = {} if args.no_tags else vm_tags(content, vms)
        now = datetime.now(timezone.utc)
        states: Dict[str, int] = {}
        for vm in vms:
            states[vm_state(vm)] = states.get(vm_state(vm), 0) + 1
        log(f"{getattr(content.about, 'fullName', '?')}: {len(vms)} VM(s) {dict(sorted(states.items()))} "
            f"collected in {time.monotonic() - t0:.1f}s")
        bad = sum(n for s, n in states.items() if s not in ("on", "off", "suspended", "template", "unknown"))
        if bad:
            log(f"{bad} VM(s) are not connected (inaccessible/orphaned/invalid); their snapshot data may be missing", "WARN")

        if args.consolidate_needed:
            report = [{"vm": vm.name, "moid": vm._moId, "power_state": vm_state(vm), "folder": vm.folder_path,
                       "snapshots": len(flatten_snapshot_tree(vm.snapshot.rootSnapshotList if vm.snapshot else []))}
                      for vm in vms if vm.runtime.consolidationNeeded]
            if args.format == "json":
                print(json.dumps(report, indent=2))
            elif args.format == "csv":
                w = csv.DictWriter(sys.stdout, fieldnames=["vm", "moid", "power_state", "folder", "snapshots"])
                w.writeheader()
                w.writerows(report)
            else:
                for r in report:
                    print(f"{r['vm']:<40} {r['power_state']:<12} snapshots={r['snapshots']:<3} {r['folder']}")
                print(f"{len(report)} VM(s) need consolidation")
            return 0

        rows: List[SnapshotRow] = []
        for vm in vms:
            rows.extend(rows_for_vm(vm, now, tags.get(vm._moId, "")))
        rows = filter_rows(rows, older, args.min_size_gb, args.name_regex, args.vm_regex)
        key = {"age": lambda r: -r.age_days, "size": lambda r: -r.size_gb, "vm": lambda r: r.vm.lower()}[args.sort]
        rows.sort(key=key)

        if args.format == "json":
            print(json.dumps([asdict(r) for r in rows], indent=2))
        elif args.format == "csv":
            sys.stdout.write(render_csv(rows))
        else:
            print(render_table(rows))

        if not args.delete:
            return 0

        # deletions: never remove snapshots that have children (would merge the chain unexpectedly)
        targets = [r for r in rows if r.children == 0]
        skipped_parents = len(rows) - len(targets)
        if skipped_parents:
            log(f"skipping {skipped_parents} snapshot(s) that have child snapshots", "WARN")
        if len(targets) > args.max_delete:
            log(f"{len(targets)} candidates exceed --max-delete {args.max_delete}; only the first {args.max_delete} (by current sort) will be removed", "WARN")
            targets = targets[: args.max_delete]
        if args.dry_run or not args.confirm:
            for r in targets:
                print(f"DRY-RUN would delete {r.vm} / {r.snapshot} ({r.size_gb} GB, {r.age_days} d)")
            return 0 if args.dry_run else 4

        failures = 0
        for r in targets:
            vm = vim.VirtualMachine(r.vm_moid, si._stub)
            snap = find_snapshot_obj(vm, r.snapshot_id)
            if snap is None:
                log(f"{r.vm}/{r.snapshot}: snapshot vanished, skipping", "WARN")
                continue
            try:
                log(f"deleting {r.vm}/{r.snapshot} ({r.size_gb} GB)")
                wait_task(snap.RemoveSnapshot_Task(removeChildren=False))
                log(f"deleted {r.vm}/{r.snapshot}", "OK")
            except Exception as exc:
                failures += 1
                log(f"failed {r.vm}/{r.snapshot}: {exc}", "ERROR")
        return 3 if failures else 0
    finally:
        try:
            Disconnect(si)
        except Exception as exc:
            log(f"disconnect: {exc}", "WARN")


if __name__ == "__main__":
    sys.exit(main())
