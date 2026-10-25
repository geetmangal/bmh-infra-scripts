# bmh-infra-scripts

[![Licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)
[![lint](https://github.com/geetmangal/bmh-infra-scripts/actions/workflows/lint.yml/badge.svg)](https://github.com/geetmangal/bmh-infra-scripts/actions/workflows/lint.yml)
[![ShellCheck](https://img.shields.io/badge/shellcheck-clean-brightgreen.svg)](https://www.shellcheck.net/)
[![Last commit](https://img.shields.io/github/last-commit/geetmangal/bmh-infra-scripts.svg)](https://github.com/geetmangal/bmh-infra-scripts/commits/main)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)

These are infrastructure automation scripts we run in production for Proxmox VE, KVM/libvirt, BGP anycast and VMware vCenter. Each one is a single file you can read in one sitting. Every script has `--dry-run`, machine-readable output and documented exit codes, and none of them contain hard-coded secrets. You can drop them into cron, systemd or a CI pipeline as they are. They are written for the stack most hosting and platform teams run: Ubuntu 22.04/24.04, Proxmox VE 8, libvirt, BIRD 2 / VyOS and vCenter 8.

## Scripts

| Script | Platform | What it does |
|---|---|---|
| [`proxmox/pve-snapshot-rotate.sh`](#pve-snapshot-rotatesh) | Proxmox VE 8 (Bash) | Rotates snapshots for VMs and CTs by VMID or tag (keep N). Skips locked guests and guests in maintenance, can use the REST API, and posts a webhook summary. |
| [`kvm/cloud-init-vm.sh`](#cloud-init-vmsh) | KVM / libvirt (Bash) | Builds an Ubuntu 22.04/24.04 cloud-image VM in one command: checksum-verified image, qcow2 overlay, cloud-init seed, virt-install, then waits for the IP. |
| [`bgp/anycast-healthcheck.py`](#anycast-healthcheckpy) | BIRD 2 / VyOS (Python, stdlib) | Runs TCP/HTTP/DNS health checks with hysteresis and withdraws or re-announces an anycast prefix. Writes Prometheus metrics and ships a systemd unit. |
| [`vmware/vcenter-snapshot-report.py`](#vcenter-snapshot-reportpy) | vCenter 8 (Python, pyvmomi) | Reports snapshot age and size, flags VMs that need consolidation, and can delete snapshots with a safety cap. |

## Quick start

```bash
git clone https://github.com/geetmangal/bmh-infra-scripts.git
cd bmh-infra-scripts

# every script explains itself
./proxmox/pve-snapshot-rotate.sh --help
./kvm/cloud-init-vm.sh --help
./bgp/anycast-healthcheck.py --help
./vmware/vcenter-snapshot-report.py --help

# see what would happen, change nothing
./proxmox/pve-snapshot-rotate.sh --tag backup --keep 7 --dry-run
./kvm/cloud-init-vm.sh --name web1 --ssh-key ~/.ssh/id_ed25519.pub --dry-run

# lint + tests
make lint test
```

To install a script system-wide, copy it to `/usr/local/sbin`. Each script is self-contained:

```bash
sudo install -m 0755 proxmox/pve-snapshot-rotate.sh /usr/local/sbin/
```

## Requirements

| Script | Needs |
|---|---|
| `pve-snapshot-rotate.sh` | Bash 4.4+, `jq`, and `qm`/`pct`/`pvesh` on a PVE node, or `curl` with `--api` |
| `cloud-init-vm.sh` | Bash 4.4+, `qemu-img`, `virt-install`, `virsh`, `curl`, `sha256sum`, plus `cloud-localds` or `genisoimage`/`xorrisofs` (`apt install qemu-utils virtinst libvirt-clients cloud-image-utils`) |
| `anycast-healthcheck.py` | Python 3.8+ (standard library only), plus `birdc` or VyOS `vbash` for the matching backend |
| `vcenter-snapshot-report.py` | Python 3.8+, `pip install -r requirements.txt` (pyvmomi) |

All scripts respect `NO_COLOR=1` and turn off colour when output is not a TTY.

---

## pve-snapshot-rotate.sh

Rotating snapshots for Proxmox VE guests. On each run it creates `PREFIX-YYYYmmdd-HHMMSS` and then deletes the oldest snapshots that carry the same prefix until only `--keep` are left. Snapshots with other names, such as manual ones, are never deleted. It skips guests with a config `lock` (for example a backup or snapshot already in progress) and guests tagged `maintenance`.

```bash
# nightly rotation of everything tagged "backup", keep a week
pve-snapshot-rotate.sh --tag backup --keep 7

# two specific VMs, include RAM state, preview only
pve-snapshot-rotate.sh --vmids 100,101 --keep 3 --vmstate --dry-run

# every guest on the node except 999, JSON summary for your monitoring
pve-snapshot-rotate.sh --all --exclude 999 --json --log-file /var/log/pve-snap.log

# via the REST API (works from any host that can reach the node)
PVE_API_HOST=https://pve1.example.net:8006 \
PVE_API_TOKEN_FILE=/etc/pve-snap.token \
pve-snapshot-rotate.sh --api --node pve1 --tag backup --keep 14
```

In `--api` mode every create/delete call returns a task UPID; the script polls `/nodes/{node}/tasks/{upid}/status` until the task stops (bounded by `--timeout`) and reports the guest as failed unless `exitstatus` is `OK`. `--keep` must be at least 1.

Cron (`/etc/cron.d/pve-snapshot-rotate`):

```cron
30 1 * * * root /usr/local/sbin/pve-snapshot-rotate.sh --tag backup --keep 7 --log-file /var/log/pve-snapshot-rotate.log --notify-webhook "https://hooks.slack.com/services/XXX"
```

`--notify-webhook` sends `{"text": ..., "content": ..., "username": ...}`. Slack, Mattermost and Discord all accept that payload.

| Exit | Meaning |
|---|---|
| 0 | success |
| 1 | usage error |
| 2 | missing dependency / not a PVE node / no API token |
| 3 | one or more guests failed |
| 4 | guests skipped for lock/maintenance (only with `--strict`) |

## cloud-init-vm.sh

A single command that gives you an Ubuntu VM you can SSH into:

1. Downloads the official cloud image into a cache and verifies it against `SHA256SUMS`. Cached base images are immutable and versioned (`<image>-<sha256 prefix>.img`): when upstream publishes a new image (checked weekly) it is downloaded next to the old one and `<image>.current` is repointed, so new VMs use the newest base while existing VMs keep backing onto the file they were created from. Old base images are never modified or deleted by the script; remove one by hand only after `qemu-img info --backing-chain` shows no overlay uses it.
2. Creates a thin qcow2 overlay on top of the image and resizes it to `--disk`.
3. Generates cloud-init `user-data`, `meta-data` and `network-config`: user, SSH keys, hostname, static IP or DHCP, packages, runcmd. `qemu-guest-agent` is always installed.
4. Builds the seed ISO with `cloud-localds`, or `genisoimage`/`xorrisofs` if that is missing.
5. Runs `virt-install` with virtio disk and NIC, a guest-agent channel, a serial console and virtio-rng.
6. Waits for the IP from the guest agent or the DHCP lease, then prints the `ssh` line.

```bash
# DHCP on the default libvirt network
cloud-init-vm.sh --name web1 --ssh-key ~/.ssh/id_ed25519.pub --network default

# static IP on a host bridge, 22.04, bigger box, extra packages
cloud-init-vm.sh --name db1 --release 22.04 --vcpus 4 --ram 8192 --disk 100 \
  --bridge br0 --ip 10.0.0.21/24 --gateway 10.0.0.1 --dns 10.0.0.1 \
  --packages postgresql,htop --runcmd "systemctl enable --now postgresql"

# print every command and the generated cloud-init, change nothing
cloud-init-vm.sh --name test --ssh-key "ssh-ed25519 AAAA... me" --dry-run

# safe to re-run from automation
cloud-init-vm.sh --name web1 --ssh-key ~/.ssh/id_ed25519.pub --idempotent --json
```

| Exit | Meaning |
|---|---|
| 0 | VM created (or exists with `--idempotent`) |
| 1 | usage error |
| 2 | missing dependency |
| 3 | image download / checksum failure |
| 4 | libvirt / virt-install failure |
| 5 | domain or disk already exists |
| 6 | timed out waiting for an IP (the VM is still running) |

If something fails after the disk has been created, the overlay and seed ISO are removed so nothing is left half-built.

## anycast-healthcheck.py

Withdraws your anycast prefix when the local service dies and announces it again once the service has recovered. It runs as a daemon or as a one-shot, and it uses only the Python standard library.

- **Checks:** TCP connect; HTTP(S) status with an optional body regex and Host header; DNS through a raw UDP query, checking the rcode.
- **Hysteresis:** the prefix is withdrawn after `fail_threshold` failing rounds in a row and announced again after `ok_threshold` passing rounds in a row, so a single lost packet does not cause a flap.
- **Backends:**
  - `bird`: runs `birdc disable|enable <protocol>`. With `bird_state_file` set, it instead rewrites an include file that holds a static route and runs `birdc configure`. See [`bgp/bird-anycast.conf.example`](bgp/bird-anycast.conf.example).
  - `vyos`: runs `vbash -c` with `set|delete protocols static route <prefix> blackhole`, then commit and save.
  - `exec`: runs your own `--exec-up` / `--exec-down` hooks. The hooks get `ANYCAST_PREFIX` and `ANYCAST_ACTION` in their environment.
- **State file:** survives restarts. `--enforce-on-start` pushes the restored state back to the router at startup.
- **Prometheus textfile metrics:** `anycast_healthy`, `anycast_announced`, `anycast_check_up{check=...}`, `anycast_check_latency_ms`, `anycast_backend_errors_total`.

```bash
# print and install an example config + systemd unit
./bgp/anycast-healthcheck.py --print-example-config | sudo tee /etc/anycast-healthcheck.yaml
sudo install -m 0755 bgp/anycast-healthcheck.py /usr/local/bin/
sudo install -m 0644 bgp/anycast-healthcheck.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now anycast-healthcheck

# one round, JSON, exit 0 healthy / 1 unhealthy, report-only: never touches the
# router or the state/metrics files, so it is safe for Nagios/Zabbix/CI probes
./bgp/anycast-healthcheck.py -c /etc/anycast-healthcheck.yaml --check-once --json

# one round that really announces/withdraws (e.g. from cron instead of the daemon)
./bgp/anycast-healthcheck.py -c /etc/anycast-healthcheck.yaml --check-once --apply

# try it without touching the router
./bgp/anycast-healthcheck.py -c bgp/anycast-healthcheck.example.yaml --check-once --dry-run -v

# custom hooks instead of BIRD/VyOS
./bgp/anycast-healthcheck.py -c my.yaml --backend exec \
  --exec-up "birdc enable anycast4" --exec-down "birdc disable anycast4"
```

The config is either JSON or a small YAML subset (maps, lists of maps, scalars and inline lists), so PyYAML is not needed. See [`bgp/anycast-healthcheck.example.yaml`](bgp/anycast-healthcheck.example.yaml) and [`.json`](bgp/anycast-healthcheck.example.json).

With `interval: 2` and `fail_threshold: 3`, the prefix is withdrawn about 6 s after the service fails. The total failover time is that plus your BGP convergence time.

The announced/withdrawn state only changes after the router command succeeds. If `birdc`, `vbash` or an exec hook fails, the state stays as it was, the action is retried every interval, and `anycast_backend_action_failed` is 1 (with `anycast_backend_errors_total` counting attempts) until it goes through.

| Exit | Meaning |
|---|---|
| 0 | healthy (`--check-once`) / clean shutdown |
| 1 | unhealthy (`--check-once`) |
| 2 | configuration error |
| 3 | backend command failed |

## vcenter-snapshot-report.py

Finds forgotten snapshots before they fill a datastore. It lists every VM snapshot along with its age, the space it holds (from `layoutEx`: its state and memory files plus the delta disks written after it, never the base disk), the VM's state (on, off, suspended, template, or inaccessible/orphaned), folder path and custom-attribute tags, and whether the VM needs consolidation. The inventory is fetched with one paged PropertyCollector call, so a 2,500-VM vCenter is read in about 4 seconds.

vCenter's default certificate is signed by its own VMCA. Pass `--ca-file` with the VMCA root certificate (from `https://<vcenter>/certs/download.zip`), or `--insecure` to skip verification. Tested read-only against vCenter 8.0.3; see [docs/testing.md](docs/testing.md).

```bash
export VC_HOST=vcenter.example.net VC_USER='svc-report@vsphere.local'
# self-signed VMCA cert: --ca-file vmca-root.pem (or --insecure)
# password: VC_PASS env, --password-file (0600) or interactive prompt

# everything older than a week, as a table
./vmware/vcenter-snapshot-report.py --password-file ~/.vc-pass --older-than 7d

# big and old, to CSV for the change ticket
./vmware/vcenter-snapshot-report.py --older-than 14d --min-size-gb 20 --format csv > old-snaps.csv

# VMs flagged "consolidation needed"
./vmware/vcenter-snapshot-report.py --consolidate-needed --format json

# delete: preview first, then for real with a cap
./vmware/vcenter-snapshot-report.py --older-than 30d --name-regex '^(auto|veeam)' --delete --dry-run
./vmware/vcenter-snapshot-report.py --older-than 30d --name-regex '^(auto|veeam)' --delete --confirm --max-delete 10
```

Deletion is protected in several ways. `--delete` on its own only previews. `--confirm` is required to actually remove anything. `--max-delete` (default 20) caps each run. Snapshots that have children are never removed, so a chain cannot be collapsed by surprise.

| Exit | Meaning |
|---|---|
| 0 | success |
| 1 | usage error |
| 2 | pyvmomi missing / connection failure |
| 3 | one or more deletions failed |
| 4 | `--delete` without `--confirm` (nothing changed) |

---

## Security notes

- **No secrets in code, arguments or logs.** Tokens and passwords come from environment variables (`PVE_API_TOKEN`, `VC_PASS`) or from `0600` files (`PVE_API_TOKEN_FILE`, `--password-file`). The scripts warn when such a file can be read by other users.
- Use a **least-privilege** account for each tool:
  - Proxmox: an API token with `VM.Snapshot` and `VM.Audit` only.
  - vCenter: a role with `VirtualMachine.State.RemoveSnapshot` plus read-only access.
- `--insecure` and `PVE_API_INSECURE=1` turn off TLS verification. Use them in a lab only.
- Anyone who can edit the `anycast-healthcheck` config can run commands through the `exec` backend, so keep the config owned by root with mode `0600`.
- To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Contributing

Pull requests are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) first. In short:

- Keep scripts safe by default and idempotent.
- `shellcheck` and `ruff` must pass.
- Add tests for pure logic.
- Use documentation IP ranges in examples.

Run `make lint test` before you push. Bugs go in an [issue](.github/ISSUE_TEMPLATE/bug.md).

## Licence

[MIT](LICENSE) © 2026 Blazing Bullet Pvt Ltd (BookMyHost).

---

## Infrastructure Partner

These scripts are maintained by the engineering team at [BookMyHost](https://bookmyhost.com), an Indian cloud provider running NVMe-backed KVM VPS on Apache CloudStack with Ceph storage and a BGP edge in Noida and Chennai.

Deep-dive posts on how we use these scripts are coming soon on [bookmyhost.com](https://bookmyhost.com).
