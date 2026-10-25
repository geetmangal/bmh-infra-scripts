# Contributing

Thanks for helping make these scripts better. Small, focused pull requests are easiest to review.

## Ground rules

- **Every script must stay safe by default**: `--dry-run` must print every mutating command and change nothing; destructive paths need an explicit flag (`--confirm`, `--delete`) and a safety cap.
- **No secrets in code or examples.** Credentials come from environment variables or 0600 files. Use RFC 5737 / 3849 documentation addresses (`203.0.113.0/24`, `2001:db8::/32`) in examples.
- **Idempotent**: running a script twice must not create duplicates or fail on the second run.
- Bash: `set -Eeuo pipefail`, trap-based cleanup, `shellcheck -S style` clean (justify any `# shellcheck disable` inline), `NO_COLOR` respected, `--help` and `--version`.
- Python: 3.8+ compatible, standard library preferred (only `vmware/` may depend on pyvmomi), `ruff check` clean, unit tests for pure logic in `tests/`.
- Document exit codes in the script header and the README.

## Workflow

1. Fork and branch from `main` (`feat/<topic>` or `fix/<topic>`).
2. `make lint test` must pass locally (`pip install ruff`; `apt install shellcheck`).
3. Add or update the README section and `CHANGELOG.md` (Unreleased) for user-visible changes.
4. Open a PR describing the environment you tested on (PVE / libvirt / BIRD / VyOS / vCenter versions).

## Adding a new script

- One folder per platform (`proxmox/`, `kvm/`, `bgp/`, `vmware/`, ...), one script per concern.
- Reuse the logging/colour/cleanup boilerplate from an existing script of the same language.
- Add a row to the scripts table in `README.md` and a usage section with at least three real examples.

## Reporting bugs and security issues

Bugs: open an issue using the template. Security issues: see `SECURITY.md`, do not open a public issue.

## Licence

By contributing you agree that your contributions are licensed under the MIT licence of this repository.
