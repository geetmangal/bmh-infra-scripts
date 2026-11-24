# bng — accel-ppp / VyOS BNG tooling

## ipoe-leak-reaper.py

Detects and (optionally) reaps **leaked accel-ppp IPoE sessions** — entries the
`ipoe` kernel module still holds after the accel daemon has forgotten them.

### The failure it fixes

A leaked module entry keeps owning a subscriber's `(parent-iface, MAC)` slot and
its `/32`. Every re-auth for that one client then fails with
`ipoe: nl_modify: File exists` and the session flaps forever — RADIUS
`authentication succeeded` every few seconds but it never reaches `active`, and
`show ipoe-server sessions` catches it stuck in `start`. Only that one client is
affected. `ip link delete`, `ip link set down` and `accel-cmd terminate` do **not**
clear it (the device is owned by the module and the daemon has no session to
terminate). The clean fix is the genl `IPOE_CMD_DELETE` the daemon itself would
send on teardown, targeting only the leaked ifindex.

### How it works

1. Read-only `IPOE_CMD_GET` dump of the `ipoe` genl family (`IPoE`) → every
   ifindex/peer the **module** holds.
2. `accel-cmd -p <port> show sessions ifname` → ifindexes the **daemon** tracks.
3. Leak = in module, not in daemon. A leak is only **reaped** once it has
   persisted for `--min-age` seconds (state file between runs), so an in-flight
   session is never deleted.

### Usage

```sh
# Report only (read-only, safe) — default:
./ipoe-leak-reaper.py --port 2002

# Machine-readable:
./ipoe-leak-reaper.py --port 2002 --json

# Reap confirmed leaks (needs CAP_NET_ADMIN / root):
sudo ./ipoe-leak-reaper.py --port 2002 --reap --log /var/log/ipoe-leak-reaper.log
```

Exit codes: `0` clean / all reaped, `1` leaks present (report mode), `2` error.
The accel ipoe CLI port on the BMH BNGs is **2002** (pppoe is 2001).

### Scheduling

Ships in **report mode**. Two options:

* **systemd** (generic): install `ipoe-leak-reaper.service` + `.timer`, then
  `systemctl enable --now ipoe-leak-reaper.timer`. Add `--reap` to the service's
  `ExecStart` to enable automatic reaping.
* **VyOS task-scheduler** (survives image upgrade; script lives in
  `/config/scripts/`):

  ```
  set system task-scheduler task ipoe-leak-reaper interval 2m
  set system task-scheduler task ipoe-leak-reaper executable path /config/scripts/ipoe-leak-reaper.py
  set system task-scheduler task ipoe-leak-reaper executable arguments '--port 2002 --quiet --log /var/log/ipoe-leak-reaper.log'
  ```

  Append `--reap` to the arguments once you want it to act, not just report.

Deploy the script to each BNG as `/config/scripts/ipoe-leak-reaper.py` (chmod +x).
Requires `python3` + `pyroute2` (already present on VyOS 1.5).
