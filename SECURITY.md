# Security policy

## Supported versions

Only the latest release on `main` is supported. Pin a tag in production and watch the changelog.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security problems.

Email **support@bookmyhost.com** with:

- the script and commit/tag affected,
- a description and reproduction steps,
- impact as you understand it.

We acknowledge reports within 3 business days and aim to ship a fix within 14 days for high-severity issues. We will credit you in the changelog unless you prefer otherwise.

## Secrets handling in these scripts

- No credentials are hard-coded. Tokens and passwords are read from environment variables (`PVE_API_TOKEN`, `VC_PASS`) or from files you create with mode `0600` (`PVE_API_TOKEN_FILE`, `--password-file`). Scripts warn when such a file is group/world readable.
- Nothing is logged that contains a secret; webhook payloads carry counts only.
- `--insecure` / `PVE_API_INSECURE=1` disable TLS verification and are meant for lab use only.
- `anycast-healthcheck.py --backend exec` runs operator-supplied shell strings; treat the config file as root-owned (`0600`) because whoever can edit it can run commands as the service user.
- The systemd unit ships with `ProtectSystem=strict`, `NoNewPrivileges` and `PrivateTmp`; narrow `ReadWritePaths` to what your deployment needs.
