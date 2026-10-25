# Real-world testing

## vcenter-snapshot-report.py, 25 Oct 2026 (IST)

Read-only report runs against four production vCenter Server instances. No snapshot was
created, changed or deleted: only the report modes were run (`table`, `--format csv`,
`--format json`, `--older-than 30d`, `--min-size-gb 10`, `--consolidate-needed`,
`--no-tags --sort size`). `--delete` was not run, not even with `--dry-run`.
All four use self-signed VMCA certificates, so `--insecure` was used. Clients were Python 3.12
on Ubuntu (WSL) and Python 3.14 on Windows 11, both with pyvmomi 8.

| vCenter | Version | VMs | VM states | Snapshots | VMs with snapshots | Snapshot GB | Runtime per mode |
|---|---|---:|---|---:|---:|---:|---:|
| A | 8.0.3 build 25197330 | 2,514 | 2,285 on, 229 off | 442-443 | 62-63 | ~3,400 | 3.8-4.1 s |
| B | 8.0.3 build 24674346 | 1,195 | 1,132 on, 43 off, 14 suspended, 5 inaccessible, 1 disconnected | 9 | 3 | 266 | 2.9-3.4 s |
| C | 8.0.3 build 24674346 | 198 | 89 on, 109 off | 17 | 5 | 938 | 0.5 s |
| D | 8.0.3 build 25413364 | 62 | 52 on, 10 off | 2 | 2 | 166 | 0.8-1.6 s |

`--consolidate-needed` found 0 VMs on all four. The snapshot count on A changed by one
between runs because the inventory is live.

### Bugs found and fixed

1. **Snapshot size included the base disk.** `layoutEx.snapshot[].disk[].chain` holds the
   full chain at the time of the snapshot, so every snapshot was reported at roughly the
   size of the whole VM (for example about 150 GB each for seven small snapshots on one VM).
   Size is now the snapshot's state/memory files plus the delta disks written after it: the
   child snapshot's new chain files, or the live disk chain for the current snapshot. Disks
   added after a snapshot are not counted.
2. **Slow on real inventories.** Each VM property was a separate SOAP round trip, and the
   tag lookup walked the inventory a second time. On B (1,195 VMs) a run took 123 s. One
   paged PropertyCollector call over a ContainerView (VMs, plus folders for the path) brings
   the same run down to 3.4 s, and 2,514 VMs take under 4 s.
3. **Inaccessible, orphaned and disconnected VMs** return almost no properties. They are
   now reported with their connection state instead of a power state, and a warning gives
   their count.
4. **Templates** were shown with a power state of "off". They now show `template`.
5. **Snapshot refs were compared by object identity**, which fails when the
   PropertyCollector returns different proxy objects for the same snapshot. They are now
   matched by MoRef id.
6. **Self-signed certificates**: added `--ca-file` (VMCA root bundle) alongside
   `--insecure`, with a clear error when TLS verification fails.
7. **Windows**: the 0600 check on `--password-file` always warned, because NTFS reports
   group/other mode bits. The check is now skipped on Windows.

Each fix has a unit test with sanitised fake objects in `tests/test_vcenter_report.py`.
