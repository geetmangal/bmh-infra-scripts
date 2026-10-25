# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/) per script (`--version`).

## [Unreleased]

## [1.0.0] - 2026-10-25

### Added
- `proxmox/pve-snapshot-rotate.sh` 1.0.0: rotating snapshots for PVE VMs/CTs via `qm`/`pct` or the REST API (`--api`), tag/VMID selection, keep-N rotation, lock and maintenance-tag skipping, JSON summary, log file, Slack/Discord/Mattermost webhook.
- `kvm/cloud-init-vm.sh` 1.0.0: one-command Ubuntu 22.04/24.04 cloud-image VM on libvirt with checksum-verified image cache, qcow2 overlay, generated cloud-init user/meta/network data, seed ISO, virt-install with virtio + guest agent, IP wait and SSH hint.
- `bgp/anycast-healthcheck.py` 1.0.0: stdlib-only daemon/one-shot health checker (TCP, HTTP, raw-UDP DNS) with hysteresis that toggles BIRD 2, VyOS or custom hooks; state file, Prometheus textfile metrics, systemd unit, example configs, unit tests.
- `vmware/vcenter-snapshot-report.py` 1.0.0: vCenter snapshot inventory with age and layoutEx size, filters, table/CSV/JSON, guarded deletion (`--delete --confirm --max-delete`), `--consolidate-needed` report, unit tests.
- Repository scaffolding: README, README-TEMPLATE, MIT licence, CONTRIBUTING, SECURITY, GitHub Actions lint workflow, issue template, Makefile.

### Fixed (before the first tag, found in real-world testing and review)
- `vmware/vcenter-snapshot-report.py`: snapshot size no longer includes the base disk (it is now state/memory files plus the delta disks written after the snapshot); inventory read with one paged PropertyCollector call instead of per-VM round trips (1,195 VMs: 123 s -> 3.4 s); inaccessible/orphaned/disconnected VMs and templates reported with their real state; snapshot refs matched by MoRef id; new `--ca-file`; no false 0600 warning on Windows. Tested read-only against four vCenter 8.0.3 instances (see `docs/testing.md`).
- `kvm/cloud-init-vm.sh`: the weekly image refresh overwrote the cached base image in place, corrupting VMs whose overlays back onto it. Base images are now immutable and versioned (`<image>-<sha256 prefix>.img`, newest selected via `<image>.current`); a refresh downloads a new file and never modifies or deletes an old one.
- `bgp/anycast-healthcheck.py`: the announce/withdraw state now commits only after the backend command succeeds; failed actions are retried every interval and exposed as `anycast_backend_action_failed`. `--check-once` is report-only by default (no router change, no state or metrics writes); new `--apply` for one-shot actuation.
- `proxmox/pve-snapshot-rotate.sh`: `--api` mode polls `/nodes/{node}/tasks/{upid}/status` until the task stops (bounded by `--timeout`) and fails the guest unless `exitstatus` is `OK`; `--keep` below 1 is rejected.
- README: removed links to the unpublished blog, corrected the Infrastructure Partner description.

