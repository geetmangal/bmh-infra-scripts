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
