#!/usr/bin/env python3
"""anycast-healthcheck.py - withdraw / announce an anycast prefix based on local service health.

Part of bmh-infra-scripts (https://github.com/geetmangal/bmh-infra-scripts)
Maintained by the BookMyHost engineering team - https://bookmyhost.com/blog
Licence: MIT.  Standard library only (Python 3.8+), no pip dependencies.

How it works
------------
Every ``interval`` seconds each configured check runs (tcp / http / dns).  A
hysteresis state machine turns raw results into a desired announcement state:

* ``fail_threshold`` consecutive failing rounds  -> WITHDRAW the prefix
* ``ok_threshold``   consecutive passing rounds  -> ANNOUNCE the prefix

Backends
--------
bird   : ``birdc disable|enable <protocol>`` (default) or, with ``--bird-state-file``,
         rewrite an include file containing a static route and run ``birdc configure``.
vyos   : ``vbash -c`` with ``set|delete protocols static route <prefix> blackhole``
         wrapped in configure/commit/save (the route is what BGP redistributes).
exec   : run arbitrary ``--exec-up`` / ``--exec-down`` commands (shell strings).

Exit codes
----------
0  healthy (one-shot) / clean shutdown (daemon)
1  unhealthy (one-shot, ``--check-once``)
2  configuration error
3  backend command failed
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import signal
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

__version__ = "1.0.0"

LOG = logging.getLogger("anycast")


# --------------------------------------------------------------------------- logging
class ColourFormatter(logging.Formatter):
    COLOURS = {
        logging.DEBUG: "\033[2m",
        logging.INFO: "\033[34m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[41m",
    }
    RESET = "\033[0m"

    def __init__(self, colour: bool) -> None:
        super().__init__("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%dT%H:%M:%S")
        self.colour = colour

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        if self.colour:
            return f"{self.COLOURS.get(record.levelno, '')}{line}{self.RESET}"
        return line


def setup_logging(verbose: bool, quiet: bool) -> None:
    colour = sys.stderr.isatty() and not os.environ.get("NO_COLOR")
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(ColourFormatter(bool(colour)))
    LOG.addHandler(handler)
    LOG.setLevel(logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO))


# --------------------------------------------------------------------------- config
@dataclass
class CheckSpec:
    name: str
    type: str                       # tcp | http | dns
    host: str = "127.0.0.1"
    port: int = 0
    timeout: float = 2.0
    # http
    url: str = ""
    expect_status: List[int] = field(default_factory=lambda: [200])
    body_regex: str = ""
    verify_tls: bool = True
    # dns
    qname: str = "."
    qtype: str = "A"
    expect_rcode: int = 0


@dataclass
class Config:
    prefix: str = ""
    interval: float = 5.0
    fail_threshold: int = 3
    ok_threshold: int = 2
    checks: List[CheckSpec] = field(default_factory=list)
    state_file: str = "/run/anycast-healthcheck.state"
    metrics_file: str = ""
    backend: str = "bird"
    bird_protocol: str = "anycast"
    bird_state_file: str = ""
    birdc: str = "birdc"
    vyos_vbash: str = "/bin/vbash"
    exec_up: str = ""
    exec_down: str = ""
    start_state: str = "auto"       # auto | announced | withdrawn


_CHECK_FIELDS = {f for f in CheckSpec.__dataclass_fields__}  # type: ignore[attr-defined]
_CONFIG_FIELDS = {f for f in Config.__dataclass_fields__}  # type: ignore[attr-defined]
_QTYPES = {"A": 1, "NS": 2, "CNAME": 5, "SOA": 6, "PTR": 12, "MX": 15, "TXT": 16, "AAAA": 28}


class ConfigError(ValueError):
    pass


def _parse_simple_yaml(text: str) -> Dict[str, Any]:
    """Tiny YAML subset parser (maps, lists of maps, scalars) so we need no PyYAML.

    Supports two-space indentation, ``key: value``, ``- key: value`` list items,
    ``[a, b]`` inline lists, quoted strings, ints, floats, bools, null and comments.
    """
    def scalar(v: str) -> Any:
        v = v.strip()
        if v == "" or v in ("null", "~"):
            return None
        if v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            return [scalar(x) for x in inner.split(",")] if inner else []
        if (v[0] == v[-1]) and v[0] in "\"'":
            return v[1:-1]
        if v.lower() in ("true", "yes", "on"):
            return True
        if v.lower() in ("false", "no", "off"):
            return False
        try:
            return int(v)
        except ValueError:
            pass
        try:
            return float(v)
        except ValueError:
            return v

    lines = []
    for raw in text.splitlines():
        stripped = raw.split(" #", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if stripped.strip():
            lines.append(stripped)

    def parse_block(idx: int, indent: int) -> Tuple[Any, int]:
        result: Any = None
        while idx < len(lines):
            line = lines[idx]
            cur = len(line) - len(line.lstrip())
            if cur < indent:
                break
            if cur > indent:
                raise ConfigError(f"bad indentation at line: {line!r}")
            body = line.strip()
            if body.startswith("- "):
                if result is None:
                    result = []
                if not isinstance(result, list):
                    raise ConfigError(f"mixed list/map at: {line!r}")
                item_body = body[2:]
                if ":" in item_body and not item_body.startswith(("'", '"', "[")):
                    # list item is a map; re-parse this line as the first key at a deeper indent
                    lines[idx] = " " * (indent + 2) + item_body
                    sub, idx = parse_block(idx, indent + 2)
                    result.append(sub)
                else:
                    result.append(scalar(item_body))
                    idx += 1
            else:
                if result is None:
                    result = {}
                if not isinstance(result, dict):
                    raise ConfigError(f"mixed list/map at: {line!r}")
                if ":" not in body:
                    raise ConfigError(f"expected key: value at: {line!r}")
                key, _, val = body.partition(":")
                key = key.strip()
                if val.strip() == "":
                    idx += 1
                    if idx < len(lines):
                        nxt = len(lines[idx]) - len(lines[idx].lstrip())
                        if nxt > indent:
                            sub, idx = parse_block(idx, nxt)
                            result[key] = sub
                            continue
                    result[key] = None
                else:
                    result[key] = scalar(val)
                    idx += 1
        return result if result is not None else {}, idx

    parsed, _ = parse_block(0, 0)
    return parsed if isinstance(parsed, dict) else {}


def load_config(path: str) -> Config:
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    if path.endswith(".json") or text.lstrip().startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid JSON in {path}: {exc}") from exc
    else:
        data = _parse_simple_yaml(text)
    return config_from_dict(data)


def config_from_dict(data: Dict[str, Any]) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("config root must be a mapping")
    cfg = Config()
    for key, val in data.items():
        if key == "checks":
            continue
        if key not in _CONFIG_FIELDS:
            raise ConfigError(f"unknown config key: {key}")
        setattr(cfg, key, val)
    checks = data.get("checks") or []
    if not isinstance(checks, list) or not checks:
        raise ConfigError("config needs a non-empty 'checks' list")
    for i, raw in enumerate(checks):
        if not isinstance(raw, dict):
            raise ConfigError(f"check #{i} must be a mapping")
        unknown = set(raw) - _CHECK_FIELDS
        if unknown:
            raise ConfigError(f"check #{i}: unknown keys {sorted(unknown)}")
        spec = CheckSpec(name=str(raw.get("name", f"check{i}")), type=str(raw.get("type", "")))
        for k, v in raw.items():
            if k in ("name", "type"):
                continue
            setattr(spec, k, [v] if (k == "expect_status" and isinstance(v, int)) else v)
        if spec.type not in ("tcp", "http", "dns"):
            raise ConfigError(f"check {spec.name}: type must be tcp|http|dns")
        if spec.type == "http" and not spec.url:
            raise ConfigError(f"check {spec.name}: http check needs 'url'")
        if spec.type in ("tcp", "dns") and not (1 <= int(spec.port or (53 if spec.type == "dns" else 0)) <= 65535):
            raise ConfigError(f"check {spec.name}: port out of range")
        if spec.type == "dns":
            spec.port = int(spec.port or 53)
            if spec.qtype.upper() not in _QTYPES:
                raise ConfigError(f"check {spec.name}: unsupported qtype {spec.qtype}")
        if spec.body_regex:
            try:
                re.compile(spec.body_regex)
            except re.error as exc:
                raise ConfigError(f"check {spec.name}: bad body_regex: {exc}") from exc
        cfg.checks.append(spec)
    if cfg.fail_threshold < 1 or cfg.ok_threshold < 1:
        raise ConfigError("fail_threshold and ok_threshold must be >= 1")
    if cfg.interval <= 0:
        raise ConfigError("interval must be > 0")
    if cfg.backend not in ("bird", "vyos", "exec"):
        raise ConfigError("backend must be bird|vyos|exec")
    if cfg.start_state not in ("auto", "announced", "withdrawn"):
        raise ConfigError("start_state must be auto|announced|withdrawn")
    if cfg.backend in ("bird", "vyos") and not cfg.prefix and (cfg.backend == "vyos" or cfg.bird_state_file):
        raise ConfigError("'prefix' is required for this backend configuration")
    return cfg


# --------------------------------------------------------------------------- checks
@dataclass
class CheckResult:
    name: str
    ok: bool
    latency_ms: float
    detail: str = ""


def check_tcp(spec: CheckSpec) -> CheckResult:
    t0 = time.monotonic()
    try:
        with socket.create_connection((spec.host, int(spec.port)), timeout=spec.timeout):
            pass
        return CheckResult(spec.name, True, (time.monotonic() - t0) * 1000, "connected")
    except OSError as exc:
        return CheckResult(spec.name, False, (time.monotonic() - t0) * 1000, f"{exc}")


def check_http(spec: CheckSpec) -> CheckResult:
    t0 = time.monotonic()
    ctx = None
    if spec.url.startswith("https://") and not spec.verify_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    if not spec.url.startswith(("http://", "https://")):
        return CheckResult(spec.name, False, 0.0, "url must start with http:// or https://")
    req = urllib.request.Request(spec.url, headers={"User-Agent": f"anycast-healthcheck/{__version__}"})  # noqa: S310 - scheme checked above
    if spec.host and spec.host != "127.0.0.1":
        req.add_header("Host", spec.host)
    try:
        with urllib.request.urlopen(req, timeout=spec.timeout, context=ctx) as resp:  # noqa: S310 - scheme checked above
            status = resp.status
            body = resp.read(65536).decode("utf-8", "replace") if spec.body_regex else ""
    except urllib.error.HTTPError as exc:
        status, body = exc.code, (exc.read(65536).decode("utf-8", "replace") if spec.body_regex else "")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return CheckResult(spec.name, False, (time.monotonic() - t0) * 1000, f"{exc}")
    lat = (time.monotonic() - t0) * 1000
    if status not in spec.expect_status:
        return CheckResult(spec.name, False, lat, f"status {status} not in {spec.expect_status}")
    if spec.body_regex and not re.search(spec.body_regex, body):
        return CheckResult(spec.name, False, lat, "body regex did not match")
    return CheckResult(spec.name, True, lat, f"status {status}")


def build_dns_query(qname: str, qtype: str, txid: int) -> bytes:
    """Build a raw DNS query packet (RD=1)."""
    header = struct.pack("!HHHHHH", txid, 0x0100, 1, 0, 0, 0)
    q = b""
    for label in qname.rstrip(".").split("."):
        if label:
            if len(label) > 63:
                raise ValueError("label too long")
            q += bytes([len(label)]) + label.encode("ascii")
    q += b"\x00" + struct.pack("!HH", _QTYPES[qtype.upper()], 1)
    return header + q


def parse_dns_response(data: bytes, txid: int) -> Tuple[int, int]:
    """Return (rcode, answer_count); raises ValueError on a malformed/mismatched reply."""
    if len(data) < 12:
        raise ValueError("short DNS response")
    rid, flags, _qd, an, _ns, _ar = struct.unpack("!HHHHHH", data[:12])
    if rid != txid:
        raise ValueError("transaction id mismatch")
    if not flags & 0x8000:
        raise ValueError("not a response")
    return flags & 0x000F, an


def check_dns(spec: CheckSpec) -> CheckResult:
    t0 = time.monotonic()
    txid = int.from_bytes(os.urandom(2), "big")
    try:
        pkt = build_dns_query(spec.qname, spec.qtype, txid)
        fam = socket.AF_INET6 if ":" in spec.host else socket.AF_INET
        with socket.socket(fam, socket.SOCK_DGRAM) as s:
            s.settimeout(spec.timeout)
            s.sendto(pkt, (spec.host, int(spec.port)))
            data, _ = s.recvfrom(4096)
        rcode, an = parse_dns_response(data, txid)
    except (OSError, ValueError) as exc:
        return CheckResult(spec.name, False, (time.monotonic() - t0) * 1000, f"{exc}")
    lat = (time.monotonic() - t0) * 1000
    if rcode != spec.expect_rcode:
        return CheckResult(spec.name, False, lat, f"rcode {rcode} != {spec.expect_rcode}")
    return CheckResult(spec.name, True, lat, f"rcode {rcode} answers {an}")


CHECKERS = {"tcp": check_tcp, "http": check_http, "dns": check_dns}


def run_checks(cfg: Config) -> List[CheckResult]:
    return [CHECKERS[c.type](c) for c in cfg.checks]


# --------------------------------------------------------------------------- hysteresis
class Hysteresis:
    """Pure state machine: feed round results, read ``announced`` and ``transition``."""

    ANNOUNCED = "announced"
    WITHDRAWN = "withdrawn"

    def __init__(self, fail_threshold: int, ok_threshold: int, initial: str = ANNOUNCED) -> None:
        if fail_threshold < 1 or ok_threshold < 1:
            raise ValueError("thresholds must be >= 1")
        self.fail_threshold = fail_threshold
        self.ok_threshold = ok_threshold
        self.state = initial
        self.consecutive_fail = 0
        self.consecutive_ok = 0

    @property
    def announced(self) -> bool:
        return self.state == self.ANNOUNCED

    def feed(self, healthy: bool) -> Optional[str]:
        """Record one round. Returns 'announce' / 'withdraw' when the state flips, else None."""
        if healthy:
            self.consecutive_ok += 1
            self.consecutive_fail = 0
            if self.state == self.WITHDRAWN and self.consecutive_ok >= self.ok_threshold:
                self.state = self.ANNOUNCED
                return "announce"
        else:
            self.consecutive_fail += 1
            self.consecutive_ok = 0
            if self.state == self.ANNOUNCED and self.consecutive_fail >= self.fail_threshold:
                self.state = self.WITHDRAWN
                return "withdraw"
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {"state": self.state, "consecutive_fail": self.consecutive_fail, "consecutive_ok": self.consecutive_ok}

    def load(self, d: Dict[str, Any]) -> None:
        if d.get("state") in (self.ANNOUNCED, self.WITHDRAWN):
            self.state = d["state"]
        self.consecutive_fail = int(d.get("consecutive_fail", 0))
        self.consecutive_ok = int(d.get("consecutive_ok", 0))


# --------------------------------------------------------------------------- backends
class BackendError(RuntimeError):
    pass


def _run(cmd: List[str], dry_run: bool, input_text: Optional[str] = None) -> str:
    LOG.debug("exec: %s", " ".join(shlex.quote(c) for c in cmd))
    if dry_run:
        return ""
    try:
        proc = subprocess.run(cmd, input=input_text, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackendError(f"{cmd[0]}: {exc}") from exc
    if proc.returncode != 0:
        raise BackendError(f"{' '.join(cmd)} exited {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


class Backend:
    name = "base"

    def __init__(self, cfg: Config, dry_run: bool) -> None:
        self.cfg = cfg
        self.dry_run = dry_run

    def announce(self) -> None:
        raise NotImplementedError

    def withdraw(self) -> None:
        raise NotImplementedError


class BirdBackend(Backend):
    name = "bird"

    def _write_state(self, announce: bool) -> None:
        path = self.cfg.bird_state_file
        body = (
            f"# managed by anycast-healthcheck - do not edit\n"
            f"# state: {'announced' if announce else 'withdrawn'} at {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n"
        )
        if announce:
            body += f"route {self.cfg.prefix} blackhole;\n"
        if self.dry_run:
            LOG.debug("would write %s:\n%s", path, body)
            return
        tmp = tempfile.NamedTemporaryFile("w", dir=os.path.dirname(path) or ".", delete=False)
        try:
            tmp.write(body)
            tmp.close()
            os.chmod(tmp.name, 0o644)
            os.replace(tmp.name, path)
        except OSError as exc:
            os.unlink(tmp.name)
            raise BackendError(f"cannot write {path}: {exc}") from exc

    def _toggle(self, announce: bool) -> None:
        if self.cfg.bird_state_file:
            self._write_state(announce)
            _run([self.cfg.birdc, "configure"], self.dry_run)
        else:
            _run([self.cfg.birdc, "enable" if announce else "disable", self.cfg.bird_protocol], self.dry_run)

    def announce(self) -> None:
        self._toggle(True)

    def withdraw(self) -> None:
        self._toggle(False)


class VyosBackend(Backend):
    name = "vyos"

    def _apply(self, verb: str) -> None:
        script = (
            "source /opt/vyatta/etc/functions/script-template\n"
            "configure\n"
            f"{verb} protocols static route {self.cfg.prefix} blackhole\n"
            "commit\nsave\nexit\n"
        )
        _run([self.cfg.vyos_vbash, "-c", script], self.dry_run)

    def announce(self) -> None:
        self._apply("set")

    def withdraw(self) -> None:
        self._apply("delete")


class ExecBackend(Backend):
    name = "exec"

    def _sh(self, cmd: str, what: str) -> None:
        if not cmd:
            raise BackendError(f"exec backend: no --exec-{what} command configured")
        env = dict(os.environ, ANYCAST_PREFIX=self.cfg.prefix, ANYCAST_ACTION=what)
        LOG.debug("exec: %s", cmd)
        if self.dry_run:
            return
        try:
            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30, check=False, env=env)  # noqa: S602
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BackendError(f"exec-{what}: {exc}") from exc
        if proc.returncode != 0:
            raise BackendError(f"exec-{what} exited {proc.returncode}: {proc.stderr.strip()}")

    def announce(self) -> None:
        self._sh(self.cfg.exec_up, "up")

    def withdraw(self) -> None:
        self._sh(self.cfg.exec_down, "down")


def make_backend(cfg: Config, dry_run: bool) -> Backend:
    return {"bird": BirdBackend, "vyos": VyosBackend, "exec": ExecBackend}[cfg.backend](cfg, dry_run)


# --------------------------------------------------------------------------- controller
class Controller:
    def __init__(self, cfg: Config, backend: Backend, dry_run: bool = False) -> None:
        self.cfg = cfg
        self.backend = backend
        self.dry_run = dry_run
        initial = Hysteresis.WITHDRAWN if cfg.start_state == "withdrawn" else Hysteresis.ANNOUNCED
        self.fsm = Hysteresis(cfg.fail_threshold, cfg.ok_threshold, initial)
        self.last_results: List[CheckResult] = []
        self.last_change = 0.0
        self.rounds = 0
        self.backend_errors = 0
        self._load_state()

    # state persistence
    def _load_state(self) -> None:
        if self.cfg.start_state != "auto" or not self.cfg.state_file:
            return
        try:
            with open(self.cfg.state_file, encoding="utf-8") as fh:
                data = json.load(fh)
            self.fsm.load(data.get("fsm", {}))
            self.last_change = float(data.get("last_change", 0.0))
            LOG.info("restored state %s from %s", self.fsm.state, self.cfg.state_file)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            LOG.warning("ignoring unreadable state file %s: %s", self.cfg.state_file, exc)

    def _save_state(self) -> None:
        if self.dry_run or not self.cfg.state_file:
            return
        data = {"fsm": self.fsm.to_dict(), "last_change": self.last_change, "updated": time.time(),
                "results": [r.__dict__ for r in self.last_results]}
        try:
            d = os.path.dirname(self.cfg.state_file) or "."
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".anycast-")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.cfg.state_file)
        except OSError as exc:
            LOG.warning("cannot write state file %s: %s", self.cfg.state_file, exc)

    def _write_metrics(self, healthy: bool) -> None:
        if not self.cfg.metrics_file or self.dry_run:
            return
        lines = [
            "# HELP anycast_healthy 1 when all checks pass in the last round",
            "# TYPE anycast_healthy gauge",
            f"anycast_healthy {int(healthy)}",
            "# HELP anycast_announced 1 when the prefix is announced",
            "# TYPE anycast_announced gauge",
            f"anycast_announced {int(self.fsm.announced)}",
            "# HELP anycast_last_change_timestamp_seconds unix time of the last announce/withdraw",
            "# TYPE anycast_last_change_timestamp_seconds gauge",
            f"anycast_last_change_timestamp_seconds {self.last_change}",
            "# HELP anycast_backend_errors_total backend command failures",
            "# TYPE anycast_backend_errors_total counter",
            f"anycast_backend_errors_total {self.backend_errors}",
            "# HELP anycast_check_up per-check result",
            "# TYPE anycast_check_up gauge",
            "# HELP anycast_check_latency_ms per-check latency",
            "# TYPE anycast_check_latency_ms gauge",
        ]
        for r in self.last_results:
            lbl = f'check="{r.name}",prefix="{self.cfg.prefix}"'
            lines.append(f"anycast_check_up{{{lbl}}} {int(r.ok)}")
            lines.append(f"anycast_check_latency_ms{{{lbl}}} {r.latency_ms:.2f}")
        try:
            d = os.path.dirname(self.cfg.metrics_file) or "."
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".anycast-metrics-")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
            os.replace(tmp, self.cfg.metrics_file)
        except OSError as exc:
            LOG.warning("cannot write metrics %s: %s", self.cfg.metrics_file, exc)

    def round(self) -> Tuple[bool, Optional[str]]:
        """Run all checks once, update the FSM and drive the backend. Returns (healthy, transition)."""
        self.rounds += 1
        self.last_results = run_checks(self.cfg)
        healthy = all(r.ok for r in self.last_results)
        for r in self.last_results:
            (LOG.debug if r.ok else LOG.warning)("check %-16s %s %.1fms %s", r.name, "OK  " if r.ok else "FAIL", r.latency_ms, r.detail)
        transition = self.fsm.feed(healthy)
        if transition:
            self._apply(transition)
        self._save_state()
        self._write_metrics(healthy)
        return healthy, transition

    def _apply(self, transition: str) -> bool:
        try:
            if transition == "withdraw":
                LOG.error("%d consecutive failures -> WITHDRAWING %s via %s", self.fsm.consecutive_fail, self.cfg.prefix or "prefix", self.backend.name)
                self.backend.withdraw()
            else:
                LOG.warning("%d consecutive successes -> ANNOUNCING %s via %s", self.fsm.consecutive_ok, self.cfg.prefix or "prefix", self.backend.name)
                self.backend.announce()
            self.last_change = time.time()
            return True
        except BackendError as exc:
            self.backend_errors += 1
            LOG.error("backend failure during %s: %s", transition, exc)
            return False

    def enforce_current(self) -> bool:
        """Push the FSM's current state to the backend (used at startup to converge)."""
        return self._apply("announce" if self.fsm.announced else "withdraw")

    def summary(self, healthy: bool, transition: Optional[str]) -> Dict[str, Any]:
        return {
            "prefix": self.cfg.prefix,
            "backend": self.backend.name,
            "healthy": healthy,
            "announced": self.fsm.announced,
            "transition": transition,
            "consecutive_fail": self.fsm.consecutive_fail,
            "consecutive_ok": self.fsm.consecutive_ok,
            "checks": [r.__dict__ for r in self.last_results],
            "dry_run": self.dry_run,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }


# --------------------------------------------------------------------------- CLI
EXAMPLE_CONFIG = """# anycast-healthcheck example config (YAML subset or JSON)
prefix: 203.0.113.53/32
interval: 2
fail_threshold: 3        # 3 x 2s = withdraw ~6s after the service dies
ok_threshold: 5          # 5 x 2s = announce 10s after recovery
backend: bird            # bird | vyos | exec
bird_protocol: anycast_static
# bird_state_file: /etc/bird/anycast-state.conf   # alternative: include-file mode
state_file: /run/anycast-healthcheck.state
metrics_file: /var/lib/node_exporter/textfile/anycast.prom
checks:
  - name: dns-udp
    type: dns
    host: 203.0.113.53
    port: 53
    qname: bookmyhost.com
    qtype: A
  - name: https
    type: http
    url: https://203.0.113.53/healthz
    host: www.example.com     # sent as Host header
    expect_status: [200, 204]
    body_regex: '"status":\\s*"ok"'
    verify_tls: false
  - name: tcp-853
    type: tcp
    host: 203.0.113.53
    port: 853
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="anycast-healthcheck.py",
        description="Health-check local services and withdraw/announce an anycast prefix via BIRD 2, VyOS or custom hooks.",
        epilog="Exit codes: 0 healthy/clean exit, 1 unhealthy (--check-once), 2 config error, 3 backend failure.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-c", "--config", default=os.environ.get("ANYCAST_CONFIG", "/etc/anycast-healthcheck.yaml"), help="YAML (subset) or JSON config file")
    p.add_argument("--check-once", action="store_true", help="run one round, print result, exit 0/1")
    p.add_argument("--json", action="store_true", help="JSON output (with --check-once) / JSON log lines in daemon mode")
    p.add_argument("--dry-run", action="store_true", help="never call the backend or write files")
    p.add_argument("--backend", choices=["bird", "vyos", "exec"], help="override config backend")
    p.add_argument("--prefix", help="override anycast prefix")
    p.add_argument("--exec-up", help="exec backend: command to announce")
    p.add_argument("--exec-down", help="exec backend: command to withdraw")
    p.add_argument("--interval", type=float, help="override interval seconds")
    p.add_argument("--state-file", help="override state file path")
    p.add_argument("--metrics-file", help="override Prometheus textfile path")
    p.add_argument("--enforce-on-start", action="store_true", help="push the restored/initial state to the backend at startup")
    p.add_argument("--print-example-config", action="store_true", help="print an example config and exit")
    p.add_argument("--print-systemd-unit", action="store_true", help="print a systemd unit and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    return p


SYSTEMD_UNIT = """[Unit]
Description=Anycast health check / BGP withdraw controller
After=network-online.target bird.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/anycast-healthcheck.py -c /etc/anycast-healthcheck.yaml --enforce-on-start
Restart=always
RestartSec=2
User=root
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/run /etc/bird /var/lib/node_exporter/textfile
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""


def apply_overrides(cfg: Config, args: argparse.Namespace) -> None:
    for attr in ("backend", "prefix", "exec_up", "exec_down", "interval", "state_file", "metrics_file"):
        val = getattr(args, attr, None)
        if val is not None:
            setattr(cfg, attr, val)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose, args.quiet or (args.check_once and args.json))
    if args.print_example_config:
        sys.stdout.write(EXAMPLE_CONFIG)
        return 0
    if args.print_systemd_unit:
        sys.stdout.write(SYSTEMD_UNIT)
        return 0
    try:
        cfg = load_config(args.config)
        apply_overrides(cfg, args)
        if cfg.backend == "exec" and not (cfg.exec_up and cfg.exec_down):
            raise ConfigError("exec backend needs exec_up and exec_down (config or --exec-up/--exec-down)")
    except ConfigError as exc:
        LOG.error("config: %s", exc)
        if args.json:
            print(json.dumps({"error": str(exc)}))
        return 2

    backend = make_backend(cfg, args.dry_run)
    ctl = Controller(cfg, backend, dry_run=args.dry_run)

    if args.check_once:
        healthy, transition = ctl.round()
        if args.json:
            print(json.dumps(ctl.summary(healthy, transition), indent=2))
        else:
            for r in ctl.last_results:
                print(f"{'OK  ' if r.ok else 'FAIL'} {r.name:<16} {r.latency_ms:7.1f} ms  {r.detail}")
            print(f"healthy={healthy} announced={ctl.fsm.announced} transition={transition}")
        if transition and ctl.backend_errors:
            return 3
        return 0 if healthy else 1

    stop = {"flag": False}

    def _sig(signum: int, _frame: Any) -> None:
        LOG.info("signal %d, shutting down", signum)
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    LOG.info("anycast-healthcheck %s starting: prefix=%s backend=%s interval=%ss fail=%d ok=%d checks=%d",
             __version__, cfg.prefix, cfg.backend, cfg.interval, cfg.fail_threshold, cfg.ok_threshold, len(cfg.checks))
    if args.enforce_on_start and not ctl.enforce_current():
        return 3
    while not stop["flag"]:
        t0 = time.monotonic()
        healthy, transition = ctl.round()
        if args.json:
            print(json.dumps(ctl.summary(healthy, transition)), flush=True)
        elif transition:
            LOG.info("state now %s", ctl.fsm.state)
        delay = max(0.0, cfg.interval - (time.monotonic() - t0))
        end = time.monotonic() + delay
        while not stop["flag"] and time.monotonic() < end:
            time.sleep(min(0.5, end - time.monotonic()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
