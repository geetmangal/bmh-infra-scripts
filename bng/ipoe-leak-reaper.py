#!/usr/bin/env python3
"""ipoe-leak-reaper — detect and (optionally) reap leaked accel-ppp IPoE sessions.

Background
----------
accel-ppp runs the IPoE data path as sessions inside the ``ipoe`` kernel module,
driven over a generic-netlink family (name ``IPoE``). On a normal teardown the
daemon sends ``IPOE_CMD_DELETE`` and the module drops the session + its netdev.

Occasionally a session "finishes" in the daemon but the module keeps its entry
(a daemon<->kernel desync — a leak). The stale module entry still owns that
subscriber's (parent-iface, MAC) slot and its /32, so every re-auth for that one
client fails with ``ipoe: nl_modify: File exists`` and the session flaps forever,
authenticating every few seconds but never reaching ``active``. Only that one
client is affected; the fix is to delete *only* the leaked module entry — exactly
the IPOE_CMD_DELETE the daemon would have sent — with zero impact to other
sessions and no daemon restart.

This tool lists the module's sessions (read-only IPOE_CMD_GET), compares them to
the accel daemon's session list (accel-cmd), and reports ifindexes the module
holds but the daemon has forgotten. With --reap it deletes them, but ONLY after
an entry has persisted as a leak for at least --min-age seconds across runs, so a
brand-new session that is momentarily in the module-but-not-daemon window is never
touched.

Safety
------
* Default mode is REPORT (read-only). Reaping requires --reap.
* A candidate is reaped only if: in module, NOT in daemon, AND first seen as a
  leak >= --min-age seconds ago (state file persisted between runs).
* Only genl IPOE_CMD_DELETE on the specific leaked ifindex is ever sent — never a
  daemon restart, never a bulk/flush command.

Exit codes: 0 = no leaks (or all reaped); 1 = leaks present (report mode);
2 = error.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import sys
import time

# ---- generic-netlink protocol (accel-ppp drivers/ipoe/ipoe.h) --------------
IPOE_GENL_NAME = "IPoE"
IPOE_GENL_VERSION = 0x02
IPOE_CMD_DELETE = 2
IPOE_CMD_GET = 4
# attribute ids (0-based, in header order)
IPOE_ATTR_PEER_ADDR = 2
IPOE_ATTR_IFINDEX = 6

DEFAULT_CLI_PORT = 2002  # accel ipoe [cli] tcp port on these BNGs (pppoe=2001)
DEFAULT_STATE = "/run/ipoe-leak-reaper.state"


def _build_ipoemsg():
    """Return the pyroute2 genlmsg subclass for the IPoE family."""
    from pyroute2.netlink import genlmsg

    class ipoemsg(genlmsg):
        nla_map = (
            ("IPOE_ATTR_NONE", "none"),
            ("IPOE_ATTR_ADDR", "be32"),
            ("IPOE_ATTR_PEER_ADDR", "be32"),
            ("IPOE_ATTR_GW_ADDR", "be32"),
            ("IPOE_ATTR_HWADDR", "uint32"),
            ("IPOE_ATTR_MASK", "uint32"),
            ("IPOE_ATTR_IFINDEX", "uint32"),
            ("IPOE_ATTR_LINK_IFINDEX", "uint32"),
            ("IPOE_ATTR_ETH_HDR", "uint32"),
            ("IPOE_ATTR_IP_HDR", "uint32"),
            ("IPOE_ATTR_MODE", "uint8"),
            ("IPOE_ATTR_ARP_HDR", "uint8"),
        )

    return ipoemsg


def _ip_str(val) -> str:
    if val is None:
        return ""
    try:
        return socket.inet_ntoa(struct.pack(">I", int(val) & 0xFFFFFFFF))
    except Exception:
        return str(val)


def module_sessions(sock, ipoemsg) -> dict:
    """Read-only: {ifindex: peer_ip_str} for every session the module holds."""
    from pyroute2.netlink import NLM_F_REQUEST, NLM_F_DUMP

    msg = ipoemsg()
    msg["cmd"] = IPOE_CMD_GET
    msg["version"] = IPOE_GENL_VERSION
    out = {}
    for r in sock.nlm_request(
        msg, msg_type=sock.prid, msg_flags=NLM_F_REQUEST | NLM_F_DUMP
    ):
        ifx = r.get_attr("IPOE_ATTR_IFINDEX")
        if ifx is None:
            continue
        out[int(ifx)] = _ip_str(r.get_attr("IPOE_ATTR_PEER_ADDR"))
    return out


def reap_one(sock, ipoemsg, ifindex: int) -> None:
    """Send IPOE_CMD_DELETE for a single leaked ifindex (raises on error)."""
    from pyroute2.netlink import NLM_F_REQUEST, NLM_F_ACK

    msg = ipoemsg()
    msg["cmd"] = IPOE_CMD_DELETE
    msg["version"] = IPOE_GENL_VERSION
    msg["attrs"] = [("IPOE_ATTR_IFINDEX", int(ifindex))]
    sock.nlm_request(msg, msg_type=sock.prid, msg_flags=NLM_F_REQUEST | NLM_F_ACK)


def _ifname_to_ifindex(name: str):
    try:
        with open("/sys/class/net/%s/ifindex" % name) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def daemon_ifindexes(port: int) -> set:
    """ifindexes of sessions the accel daemon currently tracks.

    Parses ``accel-cmd -p <port> show sessions ifname`` and maps each ipoeN name
    to its kernel ifindex. Names the daemon lists but that no longer exist in
    /sys are simply skipped (they cannot be a live blocker)."""
    import subprocess

    try:
        raw = subprocess.check_output(
            ["accel-cmd", "-p", str(port), "show sessions ifname"],
            text=True, timeout=30, stderr=subprocess.DEVNULL,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise RuntimeError("accel-cmd failed: %s" % exc)

    out = set()
    for line in raw.splitlines():
        tok = line.strip().split("|")[0].strip()
        if not tok.startswith("ipoe") or not tok[4:].isdigit():
            continue
        ifx = _ifname_to_ifindex(tok)
        if ifx is not None:
            out.add(ifx)
    return out


def compute_leaks(module_ifx: set, daemon_ifx: set, prev_state: dict,
                  now: float, min_age: float):
    """Pure decision logic (unit-tested without netlink).

    Returns (leaks, reapable, new_state):
      leaks    - ifindexes in the module but not the daemon (this run)
      reapable - subset first seen as a leak >= min_age seconds ago
      new_state- {str(ifindex): first_seen_epoch} to persist for next run
    """
    leaks = set(module_ifx) - set(daemon_ifx)
    new_state = {}
    reapable = set()
    for ifx in leaks:
        first_seen = prev_state.get(str(ifx), now)  # carry forward or start clock
        new_state[str(ifx)] = first_seen
        if now - first_seen >= min_age:
            reapable.add(ifx)
    return leaks, reapable, new_state


def _load_state(path: str) -> dict:
    try:
        with open(path) as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path: str, state: dict) -> None:
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, path)
    except OSError:
        pass  # best-effort; a missing state file only weakens the age guard


def _log(logfile, line: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    msg = "%s %s" % (stamp, line)
    if logfile:
        try:
            with open(logfile, "a") as fh:
                fh.write(msg + "\n")
        except OSError:
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Detect/reap leaked accel-ppp IPoE sessions")
    ap.add_argument("-p", "--port", type=int, default=DEFAULT_CLI_PORT,
                    help="accel-ppp ipoe CLI tcp port (default %d)" % DEFAULT_CLI_PORT)
    ap.add_argument("--reap", action="store_true",
                    help="delete confirmed leaks (default: report only)")
    ap.add_argument("--min-age", type=float, default=60.0,
                    help="seconds a leak must persist before it may be reaped (default 60)")
    ap.add_argument("--state", default=DEFAULT_STATE, help="state file path")
    ap.add_argument("--log", default="", help="append actions to this logfile")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--quiet", action="store_true", help="print nothing on a clean run")
    args = ap.parse_args(argv)

    if args.min_age < 0:
        print("--min-age must be >= 0", file=sys.stderr)
        return 2

    try:
        from pyroute2 import GenericNetlinkSocket
    except ImportError:
        print("pyroute2 not available", file=sys.stderr)
        return 2

    ipoemsg = _build_ipoemsg()
    sock = GenericNetlinkSocket()
    try:
        sock.bind(IPOE_GENL_NAME, ipoemsg)
    except Exception as exc:  # family absent => module not loaded
        print("cannot bind genl family %r: %s" % (IPOE_GENL_NAME, exc), file=sys.stderr)
        return 2

    try:
        mod = module_sessions(sock, ipoemsg)
        dmn = daemon_ifindexes(args.port)
    except Exception as exc:
        print("error: %s" % exc, file=sys.stderr)
        sock.close()
        return 2

    now = time.time()
    prev = _load_state(args.state)
    leaks, reapable, new_state = compute_leaks(
        set(mod), dmn, prev, now, args.min_age)

    reaped, failed = [], []
    if args.reap:
        for ifx in sorted(reapable):
            try:
                reap_one(sock, ipoemsg, ifx)
                reaped.append(ifx)
                new_state.pop(str(ifx), None)  # gone; stop tracking
                _log(args.log, "REAPED ifindex=%d peer=%s" % (ifx, mod.get(ifx, "")))
            except Exception as exc:
                failed.append(ifx)
                _log(args.log, "REAP-FAIL ifindex=%d peer=%s err=%s"
                     % (ifx, mod.get(ifx, ""), exc))
    sock.close()
    _save_state(args.state, new_state)

    pending = sorted(reapable - set(reaped) - set(failed))
    waiting = sorted(set(leaks) - set(reapable))  # seen, under the age guard

    if args.json:
        print(json.dumps({
            "module_total": len(mod),
            "daemon_total": len(dmn),
            "leaks": [{"ifindex": i, "peer": mod.get(i, "")} for i in sorted(leaks)],
            "reaped": reaped,
            "reap_failed": failed,
            "pending_reap": pending,
            "waiting_age_guard": waiting,
        }, indent=2))
    elif not (args.quiet and not leaks):
        print("module sessions: %d   daemon sessions: %d   leaks: %d"
              % (len(mod), len(dmn), len(leaks)))
        for ifx in sorted(leaks):
            tag = ("reaped" if ifx in reaped else
                   "REAP-FAILED" if ifx in failed else
                   "ready-to-reap" if ifx in reapable else
                   "within age-guard")
            print("  leak ifindex=%-10d peer=%-18s [%s]" % (ifx, mod.get(ifx, ""), tag))
        if not args.reap and reapable:
            print("  (%d reapable now; re-run with --reap to delete)" % len(reapable))

    if failed:
        return 2
    return 1 if (leaks and not args.reap) else 0


if __name__ == "__main__":
    raise SystemExit(main())
