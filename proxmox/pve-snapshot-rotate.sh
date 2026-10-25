#!/usr/bin/env bash
# pve-snapshot-rotate.sh - rotating snapshots for Proxmox VE VMs and containers.
#
# Part of bmh-infra-scripts (https://github.com/geetmangal/bmh-infra-scripts)
# Maintained by the BookMyHost engineering team - https://bookmyhost.com/blog
# Licence: MIT
#
# Exit codes:
#   0  success (all guests processed, or nothing to do)
#   1  usage / argument error
#   2  missing dependency (qm/pct/curl/jq) or not running on a PVE node
#   3  one or more guests failed (details in log / JSON)
#   4  lock or maintenance skip only, nothing failed (informational, treated as success unless --strict)

set -Eeuo pipefail

SCRIPT_NAME=$(basename "$0")
VERSION="1.0.0"

# ---------- defaults ----------
VMIDS=""
ALL=0
TAG=""
EXCLUDE=""
KEEP=7
PREFIX="auto"
VMSTATE=0
DRY_RUN=0
JSON=0
LOG_FILE=""
WEBHOOK=""
USE_API=0
STRICT=0
MAINT_TAG="maintenance"
TIMEOUT=600
API_HOST="${PVE_API_HOST:-https://127.0.0.1:8006}"
API_TOKEN="${PVE_API_TOKEN:-}"          # format: user@realm!tokenid=uuid
API_TOKEN_FILE="${PVE_API_TOKEN_FILE:-}"
API_INSECURE="${PVE_API_INSECURE:-0}"
NODE="$(hostname -s 2>/dev/null || echo localhost)"

# ---------- colours ----------
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  C_RESET=$'\e[0m'; C_RED=$'\e[31m'; C_GRN=$'\e[32m'; C_YEL=$'\e[33m'; C_BLU=$'\e[34m'; C_DIM=$'\e[2m'
else
  C_RESET=""; C_RED=""; C_GRN=""; C_YEL=""; C_BLU=""; C_DIM=""
fi

_ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }
_log() {
  local lvl="$1" col="$2"; shift 2
  local line
  line="[$(_ts)] [$lvl] $*"
  if [[ $JSON -eq 0 ]]; then
    printf '%s%s%s\n' "$col" "$line" "$C_RESET" >&2
  else
    printf '%s\n' "$line" >&2
  fi
  [[ -n "$LOG_FILE" ]] && printf '%s\n' "$line" >>"$LOG_FILE" || true
}
info()  { _log INFO  "$C_BLU" "$@"; }
ok()    { _log OK    "$C_GRN" "$@"; }
warn()  { _log WARN  "$C_YEL" "$@"; }
err()   { _log ERROR "$C_RED" "$@"; }
# shellcheck disable=SC2329  # used when DEBUG=1 is set by the operator
debug() { [[ "${DEBUG:-0}" == "1" ]] && _log DEBUG "$C_DIM" "$@" || true; }

# ---------- cleanup ----------
TMPDIR_WORK=""
# shellcheck disable=SC2329  # invoked via trap
cleanup() {
  local rc=$?
  [[ -n "$TMPDIR_WORK" && -d "$TMPDIR_WORK" ]] && rm -rf "$TMPDIR_WORK"
  if [[ $rc -ne 0 && $rc -ne 3 && $rc -ne 4 ]]; then
    err "aborted with exit code $rc (line ${BASH_LINENO[0]:-?})"
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'err "interrupted"; exit 130' INT TERM

usage() {
  cat <<EOF
$SCRIPT_NAME v$VERSION - rotating snapshots for Proxmox VE guests (qm/pct or REST API)

Usage:
  $SCRIPT_NAME (--vmids 100,101 | --all | --tag backup) [options]

Selection:
  --vmids LIST          comma-separated VMIDs
  --all                 every VM and CT on this node
  --tag TAG             guests carrying this PVE tag (e.g. backup)
  --exclude LIST        comma-separated VMIDs to skip
  --maintenance-tag T   guests with this tag are skipped (default: $MAINT_TAG)

Rotation:
  --keep N              snapshots (with --prefix) to keep per guest (default: $KEEP)
  --prefix NAME         snapshot name prefix (default: $PREFIX); name = PREFIX-YYYYmmdd-HHMMSS
  --vmstate             include RAM state for running VMs (qm only)
  --timeout SEC         per-operation timeout (default: $TIMEOUT)

Backend:
  --api                 use the REST API via curl instead of qm/pct
                        env PVE_API_HOST (default $API_HOST), PVE_API_TOKEN or
                        PVE_API_TOKEN_FILE (0600 file containing user@realm!id=secret),
                        PVE_API_INSECURE=1 to skip TLS verification
  --node NAME           node name for --api (default: $NODE)

Output:
  --dry-run             print what would happen, change nothing
  --json                emit a JSON summary on stdout (logs go to stderr)
  --log-file PATH       append log lines to PATH
  --notify-webhook URL  POST a Slack/Discord/Mattermost compatible summary
  --strict              exit 4 when guests were skipped due to locks/maintenance
  -h, --help            this help
  -V, --version         print version

Examples:
  $SCRIPT_NAME --tag backup --keep 7
  $SCRIPT_NAME --vmids 100,101 --keep 3 --vmstate --dry-run
  $SCRIPT_NAME --all --exclude 999 --json --log-file /var/log/pve-snap.log
  PVE_API_TOKEN_FILE=/etc/pve-snap.token $SCRIPT_NAME --api --tag backup

Cron (nightly 01:30):
  30 1 * * * root /usr/local/sbin/pve-snapshot-rotate.sh --tag backup --keep 7 \\
             --log-file /var/log/pve-snapshot-rotate.log --notify-webhook "\$SLACK_WEBHOOK"

Exit codes: 0 ok, 1 usage, 2 missing dependency, 3 guest failures, 4 skips (with --strict)
EOF
}

# ---------- arg parsing ----------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --vmids) VMIDS="$2"; shift 2 ;;
    --all) ALL=1; shift ;;
    --tag) TAG="$2"; shift 2 ;;
    --exclude) EXCLUDE="$2"; shift 2 ;;
    --maintenance-tag) MAINT_TAG="$2"; shift 2 ;;
    --keep) KEEP="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --vmstate) VMSTATE=1; shift ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --api) USE_API=1; shift ;;
    --node) NODE="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --json) JSON=1; shift ;;
    --log-file) LOG_FILE="$2"; shift 2 ;;
    --notify-webhook) WEBHOOK="$2"; shift 2 ;;
    --strict) STRICT=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -V|--version) echo "$VERSION"; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

# ---------- validation ----------
sel=0
[[ -n "$VMIDS" ]] && sel=$((sel+1))
[[ $ALL -eq 1 ]] && sel=$((sel+1))
[[ -n "$TAG" ]] && sel=$((sel+1))
if [[ $sel -ne 1 ]]; then
  err "exactly one of --vmids, --all or --tag is required"; exit 1
fi
[[ "$KEEP" =~ ^[0-9]+$ ]] || { err "--keep must be a non-negative integer"; exit 1; }
[[ "$PREFIX" =~ ^[A-Za-z][A-Za-z0-9_]{0,20}$ ]] || { err "--prefix must match [A-Za-z][A-Za-z0-9_]{0,20}"; exit 1; }
[[ "$TIMEOUT" =~ ^[0-9]+$ ]] || { err "--timeout must be an integer"; exit 1; }

command -v jq >/dev/null || { err "missing dependency: jq"; exit 2; }
if [[ $USE_API -eq 1 ]]; then
  command -v curl >/dev/null || { err "missing dependency: curl"; exit 2; }
  if [[ -z "$API_TOKEN" && -n "$API_TOKEN_FILE" ]]; then
    [[ -r "$API_TOKEN_FILE" ]] || { err "cannot read PVE_API_TOKEN_FILE=$API_TOKEN_FILE"; exit 2; }
    perm=$(stat -c '%a' "$API_TOKEN_FILE" 2>/dev/null || echo "")
    [[ "$perm" == "600" || "$perm" == "400" ]] || warn "$API_TOKEN_FILE should be mode 0600 (is $perm)"
    API_TOKEN=$(head -n1 "$API_TOKEN_FILE" | tr -d '[:space:]')
  fi
  [[ -n "$API_TOKEN" ]] || { err "--api needs PVE_API_TOKEN or PVE_API_TOKEN_FILE"; exit 2; }
else
  if [[ $DRY_RUN -eq 0 ]]; then
    for d in qm pct pvesh; do command -v "$d" >/dev/null || { err "missing dependency: $d (run on a PVE node or use --api)"; exit 2; }; done
  fi
fi

TMPDIR_WORK=$(mktemp -d)
SNAP_NAME="${PREFIX}-$(date '+%Y%m%d-%H%M%S')"

# ---------- backend helpers ----------
# Each returns JSON on stdout.
api() {  # method path [form-data...]
  local method="$1" path="$2"; shift 2
  local curl_opts=(-sS --fail-with-body --max-time "$TIMEOUT" -X "$method" -H "Authorization: PVEAPIToken=$API_TOKEN")
  [[ "$API_INSECURE" == "1" ]] && curl_opts+=(-k)
  local arg
  for arg in "$@"; do curl_opts+=(--data-urlencode "$arg"); done
  curl "${curl_opts[@]}" "$API_HOST/api2/json$path"
}

list_guests() {  # -> lines: vmid type name status tags
  if [[ $USE_API -eq 1 ]]; then
    api GET "/cluster/resources?type=vm" | jq -r --arg node "$NODE" \
      '.data[] | select(.node==$node) | [.vmid, .type, (.name//""), .status, (.tags//"")] | @tsv'
  elif [[ $DRY_RUN -eq 1 ]] && ! command -v pvesh >/dev/null; then
    # dry-run on a non-PVE box: fall back to the requested vmids as qemu guests
    tr ',' '\n' <<<"$VMIDS" | awk 'NF{print $1"\tqemu\tvm"$1"\tunknown\t"}'
  else
    pvesh get /cluster/resources --type vm --output-format json | jq -r --arg node "$NODE" \
      '.[] | select(.node==$node) | [.vmid, .type, (.name//""), .status, (.tags//"")] | @tsv'
  fi
}

guest_tool() { [[ "$1" == "lxc" ]] && echo pct || echo qm; }

guest_lock() {  # vmid type -> lock value or empty
  local vmid="$1" type="$2"
  if [[ $USE_API -eq 1 ]]; then
    api GET "/nodes/$NODE/$type/$vmid/config" | jq -r '.data.lock // empty'
  else
    "$(guest_tool "$type")" config "$vmid" 2>/dev/null | awk -F': ' '$1=="lock"{print $2}'
  fi
}

list_snaps() {  # vmid type -> names matching prefix, oldest first
  local vmid="$1" type="$2"
  if [[ $USE_API -eq 1 ]]; then
    api GET "/nodes/$NODE/$type/$vmid/snapshot" | jq -r --arg p "$PREFIX-" \
      '.data[] | select(.name!="current") | select(.name|startswith($p)) | [.snaptime, .name] | @tsv' \
      | sort -n | cut -f2
  else
    "$(guest_tool "$type")" listsnapshot "$vmid" 2>/dev/null \
      | awk -v p="$PREFIX-" '{for(i=1;i<=NF;i++) if(index($i,p)==1){print $i; break}}' | sort
  fi
}

create_snap() {  # vmid type name
  local vmid="$1" type="$2" name="$3"
  if [[ $USE_API -eq 1 ]]; then
    local extra=()
    [[ "$type" == "qemu" && $VMSTATE -eq 1 ]] && extra+=("vmstate=1")
    api POST "/nodes/$NODE/$type/$vmid/snapshot" "snapname=$name" "description=created by $SCRIPT_NAME" "${extra[@]}" >/dev/null
  else
    local cmd=("$(guest_tool "$type")" snapshot "$vmid" "$name" --description "created by $SCRIPT_NAME")
    [[ "$type" == "qemu" && $VMSTATE -eq 1 ]] && cmd+=(--vmstate 1)
    timeout "$TIMEOUT" "${cmd[@]}" >/dev/null
  fi
}

delete_snap() {  # vmid type name
  local vmid="$1" type="$2" name="$3"
  if [[ $USE_API -eq 1 ]]; then
    api DELETE "/nodes/$NODE/$type/$vmid/snapshot/$name" >/dev/null
  else
    timeout "$TIMEOUT" "$(guest_tool "$type")" delsnapshot "$vmid" "$name" >/dev/null
  fi
}

has_tag() { # tags-string tag
  local IFS=';'
  local t
  for t in $1; do [[ "$t" == "$2" ]] && return 0; done
  return 1
}

in_list() { # needle csv
  local IFS=','
  local x
  for x in $2; do [[ "$x" == "$1" ]] && return 0; done
  return 1
}

# ---------- main ----------
info "$SCRIPT_NAME v$VERSION starting (node=$NODE keep=$KEEP prefix=$PREFIX dry-run=$DRY_RUN api=$USE_API)"
RESULTS="$TMPDIR_WORK/results.jsonl"
: >"$RESULTS"
n_ok=0 n_fail=0 n_skip=0

while IFS=$'\t' read -r vmid type name status tags; do
  [[ -z "$vmid" ]] && continue
  if [[ -n "$VMIDS" ]] && ! in_list "$vmid" "$VMIDS"; then continue; fi
  if [[ -n "$TAG" ]] && ! has_tag "$tags" "$TAG"; then continue; fi
  if [[ -n "$EXCLUDE" ]] && in_list "$vmid" "$EXCLUDE"; then
    info "[$vmid] excluded"; continue
  fi
  if has_tag "$tags" "$MAINT_TAG"; then
    warn "[$vmid] $name has tag '$MAINT_TAG', skipping"
    n_skip=$((n_skip+1)); jq -nc --arg v "$vmid" --arg n "$name" '{vmid:$v,name:$n,result:"skipped",reason:"maintenance"}' >>"$RESULTS"; continue
  fi
  if [[ "$status" == "unknown" && $DRY_RUN -eq 0 ]]; then
    warn "[$vmid] status unknown, skipping"; n_skip=$((n_skip+1)); continue
  fi

  lock=""
  if [[ $DRY_RUN -eq 0 || $USE_API -eq 1 ]]; then lock=$(guest_lock "$vmid" "$type" || true); fi
  if [[ -n "$lock" ]]; then
    warn "[$vmid] $name is locked ($lock), skipping"
    n_skip=$((n_skip+1)); jq -nc --arg v "$vmid" --arg n "$name" --arg l "$lock" '{vmid:$v,name:$n,result:"skipped",reason:("lock:"+$l)}' >>"$RESULTS"; continue
  fi

  mapfile -t existing < <(list_snaps "$vmid" "$type" 2>/dev/null || true)
  info "[$vmid] $name ($type, $status): ${#existing[@]} existing '$PREFIX-*' snapshot(s)"

  created=""; deleted=(); failed=""
  if [[ $DRY_RUN -eq 1 ]]; then
    info "[$vmid] DRY-RUN would create snapshot $SNAP_NAME"
  else
    if create_snap "$vmid" "$type" "$SNAP_NAME"; then
      ok "[$vmid] created $SNAP_NAME"; created="$SNAP_NAME"
    else
      err "[$vmid] snapshot creation failed"; failed="create"
    fi
  fi

  if [[ -z "$failed" ]]; then
    total=$(( ${#existing[@]} + 1 ))   # existing + the one just created (or that would be)
    to_delete=$(( total - KEEP ))
    if [[ $to_delete -gt 0 ]]; then
      for ((i=0; i<to_delete && i<${#existing[@]}; i++)); do
        s="${existing[$i]}"
        if [[ $DRY_RUN -eq 1 ]]; then
          info "[$vmid] DRY-RUN would delete $s"; deleted+=("$s")
        elif delete_snap "$vmid" "$type" "$s"; then
          ok "[$vmid] deleted $s"; deleted+=("$s")
        else
          err "[$vmid] failed to delete $s"; failed="delete"
        fi
      done
    fi
  fi

  if [[ -n "$failed" ]]; then n_fail=$((n_fail+1)); res="failed"; else n_ok=$((n_ok+1)); res="ok"; fi
  jq -nc --arg v "$vmid" --arg n "$name" --arg t "$type" --arg r "$res" --arg c "$created" --arg f "$failed" \
     --argjson d "$(printf '%s\n' "${deleted[@]:-}" | jq -R . | jq -s 'map(select(length>0))')" \
     '{vmid:$v,name:$n,type:$t,result:$r,created:(if $c=="" then null else $c end),deleted:$d,error:(if $f=="" then null else $f end)}' >>"$RESULTS"
done < <(list_guests)

summary=$(jq -s -c --arg node "$NODE" --arg snap "$SNAP_NAME" --argjson dry "$DRY_RUN" \
  --argjson ok "$n_ok" --argjson fail "$n_fail" --argjson skip "$n_skip" \
  '{node:$node, snapshot:$snap, dry_run:($dry==1), ok:$ok, failed:$fail, skipped:$skip, guests:.}' "$RESULTS")

if [[ $JSON -eq 1 ]]; then printf '%s\n' "$summary"; fi
info "done: ok=$n_ok failed=$n_fail skipped=$n_skip"

if [[ -n "$WEBHOOK" && $DRY_RUN -eq 0 ]]; then
  icon=":white_check_mark:"; [[ $n_fail -gt 0 ]] && icon=":x:"
  text="$icon pve-snapshot-rotate on $NODE: ok=$n_ok failed=$n_fail skipped=$n_skip (snapshot $SNAP_NAME)"
  payload=$(jq -nc --arg t "$text" '{text:$t, content:$t, username:"pve-snapshot-rotate"}')
  curl -sS --max-time 15 -H 'Content-Type: application/json' -d "$payload" "$WEBHOOK" >/dev/null \
    || warn "webhook notification failed"
fi

[[ $n_fail -gt 0 ]] && exit 3
[[ $STRICT -eq 1 && $n_skip -gt 0 ]] && exit 4
exit 0
