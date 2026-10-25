#!/usr/bin/env bash
# cloud-init-vm.sh - create an Ubuntu cloud-image KVM/libvirt VM in one command.
#
# Part of bmh-infra-scripts (https://github.com/geetmangal/bmh-infra-scripts)
# Maintained by the BookMyHost engineering team - https://bookmyhost.com
# Licence: MIT
#
# Exit codes:
#   0  VM created (or already exists and --idempotent)
#   1  usage / argument error
#   2  missing dependency
#   3  image download or checksum failure
#   4  libvirt / virt-install failure
#   5  VM exists already (without --idempotent)
#   6  timed out waiting for an IP address (VM is still running)

set -Eeuo pipefail

SCRIPT_NAME=$(basename "$0")
VERSION="1.0.0"

# ---------- defaults ----------
NAME=""
RELEASE="24.04"
VCPUS=2
RAM_MB=2048
DISK_GB=20
BRIDGE="virbr0"
NETWORK=""                 # libvirt network name (alternative to --bridge)
USERNAME="ubuntu"
SSH_KEYS=()                # files or literal keys
HOSTNAME_OVERRIDE=""
IP_CIDR=""                 # e.g. 192.168.122.50/24 ; empty = DHCP
GATEWAY=""
DNS="1.1.1.1,8.8.8.8"
PACKAGES=""
RUNCMDS=()
USER_DATA_FILE=""
CACHE_DIR="${CLOUD_IMAGE_CACHE:-/var/lib/libvirt/images/cloud-cache}"
IMAGE_DIR="${LIBVIRT_IMAGE_DIR:-/var/lib/libvirt/images}"
OS_VARIANT=""
DRY_RUN=0
JSON=0
IDEMPOTENT=0
WAIT_SECS=180
NO_WAIT=0
CONNECT="${LIBVIRT_DEFAULT_URI:-qemu:///system}"
MIRROR="${UBUNTU_CLOUD_MIRROR:-https://cloud-images.ubuntu.com}"
PASSWORD_HASH=""           # optional, from --password-hash-file
START=1

# ---------- colours / logging ----------
if [[ -t 2 && -z "${NO_COLOR:-}" ]]; then
  C_RESET=$'\e[0m'; C_RED=$'\e[31m'; C_GRN=$'\e[32m'; C_YEL=$'\e[33m'; C_BLU=$'\e[34m'; C_DIM=$'\e[2m'
else
  C_RESET=""; C_RED=""; C_GRN=""; C_YEL=""; C_BLU=""; C_DIM=""
fi
_log() { local lvl="$1" col="$2"; shift 2; printf '%s[%s] [%s] %s%s\n' "$col" "$(date '+%H:%M:%S')" "$lvl" "$*" "$C_RESET" >&2; }
info() { _log INFO "$C_BLU" "$@"; }
ok()   { _log OK   "$C_GRN" "$@"; }
warn() { _log WARN "$C_YEL" "$@"; }
err()  { _log ERR  "$C_RED" "$@"; }

# run CMD... : execute or print
run() {
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '%s+ %s%s\n' "$C_DIM" "$(printf '%q ' "$@")" "$C_RESET" >&2
  else
    "$@"
  fi
}

# ---------- cleanup ----------
WORKDIR=""
CREATED_DISK=""
# shellcheck disable=SC2329  # invoked via trap
# invoked via trap; older shellcheck reports its body as unreachable
# shellcheck disable=SC2317,SC2015
cleanup() {
  local rc=$?
  [[ -n "$WORKDIR" && -d "$WORKDIR" ]] && rm -rf "$WORKDIR"
  if [[ $rc -ne 0 && $rc -ne 6 && -n "$CREATED_DISK" && $DRY_RUN -eq 0 ]]; then
    warn "failure after disk creation; removing $CREATED_DISK"
    rm -f "$CREATED_DISK" "${CREATED_DISK%.qcow2}-seed.iso" 2>/dev/null || true
  fi
  [[ $rc -ne 0 ]] && err "exit $rc"
  exit "$rc"
}
trap cleanup EXIT
trap 'err interrupted; exit 130' INT TERM

usage() {
  cat <<EOF
$SCRIPT_NAME v$VERSION - Ubuntu cloud-image KVM/libvirt VM in one command

Usage:
  $SCRIPT_NAME --name NAME --ssh-key ~/.ssh/id_ed25519.pub [options]

VM:
  --name NAME             libvirt domain name (required, also default hostname)
  --release 22.04|24.04   Ubuntu release (default: $RELEASE)
  --vcpus N               vCPUs (default: $VCPUS)
  --ram MB                memory in MiB (default: $RAM_MB)
  --disk GB               root disk size in GiB (default: $DISK_GB, grows cloud image)
  --bridge BR             host bridge (default: $BRIDGE)
  --network NET           libvirt network name instead of a bridge
  --os-variant V          virt-install --os-variant (auto: ubuntu22.04/ubuntu24.04)
  --connect URI           libvirt URI (default: $CONNECT)
  --image-dir DIR         where the VM qcow2 lives (default: $IMAGE_DIR)
  --cache-dir DIR         cloud image cache (default: $CACHE_DIR)
  --no-start              define but do not start

cloud-init:
  --user USER             login user (default: $USERNAME)
  --ssh-key FILE|KEY      authorised key (repeatable; file path or literal "ssh-ed25519 ...")
  --hostname H            hostname (default: --name)
  --ip CIDR               static IPv4, e.g. 192.168.122.50/24 (default: DHCP)
  --gateway IP            default gateway (required with --ip)
  --dns LIST              comma-separated resolvers (default: $DNS)
  --packages LIST         comma-separated apt packages
  --runcmd CMD            extra runcmd line (repeatable)
  --password-hash-file F  0600 file with a crypt(3) hash to set a console password
  --user-data FILE        use this user-data verbatim instead of generating one

Behaviour:
  --wait SEC              wait up to SEC for an IP (default: $WAIT_SECS); --no-wait to skip
  --idempotent            exit 0 if the domain already exists
  --dry-run               print every command, change nothing
  --json                  print a JSON result on stdout
  -h, --help | -V, --version

Environment: CLOUD_IMAGE_CACHE, LIBVIRT_IMAGE_DIR, LIBVIRT_DEFAULT_URI, UBUNTU_CLOUD_MIRROR, NO_COLOR

Examples:
  $SCRIPT_NAME --name web1 --ssh-key ~/.ssh/id_ed25519.pub
  $SCRIPT_NAME --name db1 --release 22.04 --vcpus 4 --ram 8192 --disk 100 \\
      --bridge br0 --ip 10.0.0.21/24 --gateway 10.0.0.1 --packages postgresql,qemu-guest-agent
  $SCRIPT_NAME --name test --ssh-key "ssh-ed25519 AAAA... me" --dry-run

Exit codes: 0 ok, 1 usage, 2 dependency, 3 image/checksum, 4 libvirt, 5 exists, 6 IP wait timeout
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --release) RELEASE="$2"; shift 2 ;;
    --vcpus) VCPUS="$2"; shift 2 ;;
    --ram) RAM_MB="$2"; shift 2 ;;
    --disk) DISK_GB="$2"; shift 2 ;;
    --bridge) BRIDGE="$2"; shift 2 ;;
    --network) NETWORK="$2"; shift 2 ;;
    --os-variant) OS_VARIANT="$2"; shift 2 ;;
    --connect) CONNECT="$2"; shift 2 ;;
    --image-dir) IMAGE_DIR="$2"; shift 2 ;;
    --cache-dir) CACHE_DIR="$2"; shift 2 ;;
    --no-start) START=0; shift ;;
    --user) USERNAME="$2"; shift 2 ;;
    --ssh-key) SSH_KEYS+=("$2"); shift 2 ;;
    --hostname) HOSTNAME_OVERRIDE="$2"; shift 2 ;;
    --ip) IP_CIDR="$2"; shift 2 ;;
    --gateway) GATEWAY="$2"; shift 2 ;;
    --dns) DNS="$2"; shift 2 ;;
    --packages) PACKAGES="$2"; shift 2 ;;
    --runcmd) RUNCMDS+=("$2"); shift 2 ;;
    --password-hash-file) PASSWORD_HASH=$(head -n1 "$2"); shift 2 ;;
    --user-data) USER_DATA_FILE="$2"; shift 2 ;;
    --wait) WAIT_SECS="$2"; shift 2 ;;
    --no-wait) NO_WAIT=1; shift ;;
    --idempotent) IDEMPOTENT=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --json) JSON=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -V|--version) echo "$VERSION"; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

# ---------- validation ----------
[[ -n "$NAME" ]] || { err "--name is required"; exit 1; }
[[ "$NAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$ ]] || { err "invalid --name"; exit 1; }
case "$RELEASE" in
  22.04) CODENAME="jammy"; OS_VARIANT="${OS_VARIANT:-ubuntu22.04}" ;;
  24.04) CODENAME="noble"; OS_VARIANT="${OS_VARIANT:-ubuntu24.04}" ;;
  *) err "--release must be 22.04 or 24.04"; exit 1 ;;
esac
for v in VCPUS RAM_MB DISK_GB WAIT_SECS; do
  [[ "${!v}" =~ ^[0-9]+$ ]] || { err "$v must be an integer"; exit 1; }
done
if [[ -n "$IP_CIDR" ]]; then
  [[ "$IP_CIDR" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$ ]] || { err "--ip must be a.b.c.d/nn"; exit 1; }
  [[ -n "$GATEWAY" ]] || { err "--gateway is required with --ip"; exit 1; }
fi
if [[ ${#SSH_KEYS[@]} -eq 0 && -z "$USER_DATA_FILE" && -z "$PASSWORD_HASH" ]]; then
  err "at least one --ssh-key (or --user-data / --password-hash-file) is required, otherwise the VM is unreachable"; exit 1
fi
HOSTNAME_FINAL="${HOSTNAME_OVERRIDE:-$NAME}"

# resolve ssh keys
AUTH_KEYS=()
for k in "${SSH_KEYS[@]:-}"; do
  [[ -z "$k" ]] && continue
  if [[ -f "$k" ]]; then
    while IFS= read -r line; do [[ -n "$line" ]] && AUTH_KEYS+=("$line"); done <"$k"
  elif [[ "$k" =~ ^(ssh-(rsa|ed25519|dss)|ecdsa-sha2-nistp[0-9]+|sk-) ]]; then
    AUTH_KEYS+=("$k")
  else
    err "--ssh-key '$k' is neither a readable file nor a public key"; exit 1
  fi
done

# ---------- dependencies ----------
need=(qemu-img virt-install virsh curl sha256sum)
if command -v cloud-localds >/dev/null; then SEED_TOOL=cloud-localds
elif command -v genisoimage >/dev/null; then SEED_TOOL=genisoimage
elif command -v xorrisofs >/dev/null; then SEED_TOOL=xorrisofs
else SEED_TOOL=""; fi
missing=()
for d in "${need[@]}"; do command -v "$d" >/dev/null || missing+=("$d"); done
[[ -n "$SEED_TOOL" ]] || missing+=("cloud-localds|genisoimage")
if [[ ${#missing[@]} -gt 0 ]]; then
  if [[ $DRY_RUN -eq 1 ]]; then
    warn "missing tools (ignored in dry-run): ${missing[*]}"; SEED_TOOL="${SEED_TOOL:-cloud-localds}"
  else
    err "missing dependencies: ${missing[*]}  (apt install qemu-utils virtinst libvirt-clients cloud-image-utils)"; exit 2
  fi
fi

VIRSH=(virsh -c "$CONNECT")

# ---------- idempotency ----------
if [[ $DRY_RUN -eq 0 ]] && "${VIRSH[@]}" dominfo "$NAME" >/dev/null 2>&1; then
  if [[ $IDEMPOTENT -eq 1 ]]; then ok "domain $NAME already exists, nothing to do"; exit 0; fi
  err "domain $NAME already exists (use --idempotent to treat as success)"; exit 5
fi

# ---------- image download + checksum ----------
IMG_NAME="${CODENAME}-server-cloudimg-amd64.img"
IMG_URL="$MIRROR/$CODENAME/current/$IMG_NAME"
SUMS_URL="$MIRROR/$CODENAME/current/SHA256SUMS"
IMG_STEM="${IMG_NAME%.img}"
# Base images are immutable and versioned: <stem>-<sha256[0:12]>.img.  A refresh
# downloads a NEW file and repoints <stem>.current at it; existing base files are
# never modified or deleted, because VM overlays created earlier back onto them.
CURRENT_PTR="$CACHE_DIR/$IMG_STEM.current"
BASE_IMG=""

run mkdir -p "$CACHE_DIR" "$IMAGE_DIR"
WORKDIR=$(mktemp -d)

versioned_path() { printf '%s/%s-%s.img' "$CACHE_DIR" "$IMG_STEM" "${1:0:12}"; }

upstream_sha() {  # prints the upstream sha256 of IMG_NAME; non-zero if the mirror is unreachable
  curl -fsSL --retry 3 --max-time 60 -o "$WORKDIR/SHA256SUMS" "$SUMS_URL" 2>/dev/null || return 1
  awk -v f="$IMG_NAME" '$2==f || $2==("*" f) {print $1; exit}' "$WORKDIR/SHA256SUMS"
}

set_current() {  # atomically point <stem>.current at a verified versioned image
  printf '%s\n' "$(basename "$1")" >"$CURRENT_PTR.tmp.$$" && mv -f "$CURRENT_PTR.tmp.$$" "$CURRENT_PTR"
}

fetch_and_verify() {  # $1 = expected sha256 (optional, looked up when empty)
  local expected="${1:-}" dest part actual
  if [[ $DRY_RUN -eq 1 ]]; then
    BASE_IMG="$CACHE_DIR/$IMG_STEM-<sha256>.img"
    run curl -fsSL --retry 3 -o "$BASE_IMG.part" "$IMG_URL"
    return 0
  fi
  [[ -n "$expected" ]] || expected=$(upstream_sha) || true
  [[ -n "$expected" ]] || { err "no checksum for $IMG_NAME in $SUMS_URL"; exit 3; }
  dest=$(versioned_path "$expected")
  if [[ -f "$dest" ]]; then
    info "base image $dest already cached"
  else
    part="$dest.part.$$"
    info "downloading $IMG_URL"
    curl -fsSL --retry 3 -o "$part" "$IMG_URL" || { rm -f "$part"; err "download failed: $IMG_URL"; exit 3; }
    actual=$(sha256sum "$part" | cut -d' ' -f1)
    if [[ "$expected" != "$actual" ]]; then rm -f "$part"; err "checksum mismatch: expected $expected got $actual"; exit 3; fi
    mv -n "$part" "$dest"; rm -f "$part"
    printf '%s\n' "$actual" >"$dest.sha256"
    chmod a-w "$dest" 2>/dev/null || true
    ok "cloud image cached at $dest"
  fi
  set_current "$dest"
  BASE_IMG="$dest"
}

cur=""
[[ -f "$CURRENT_PTR" ]] && cur="$CACHE_DIR/$(head -n1 "$CURRENT_PTR")"
if [[ -n "$cur" && -f "$cur" ]]; then
  BASE_IMG="$cur"
  # check upstream weekly; a newer image is downloaded next to the old one, never over it
  if [[ -n $(find "$CURRENT_PTR" -mtime +7 2>/dev/null) ]]; then
    info "cached image pointer older than 7 days, checking upstream checksum"
    if expected=$(upstream_sha) && [[ -n "$expected" ]]; then
      have=$(cat "$cur.sha256" 2>/dev/null || sha256sum "$cur" | cut -d' ' -f1)
      if [[ "$expected" != "$have" ]]; then
        info "upstream image changed, fetching a new base (old one kept for existing VMs)"
        fetch_and_verify "$expected"
      else run touch "$CURRENT_PTR"; fi
    else warn "could not reach mirror, using cached image"; fi
  else info "using cached image $BASE_IMG"; fi
else
  fetch_and_verify
fi

# ---------- disk ----------
DISK="$IMAGE_DIR/$NAME.qcow2"
SEED="$IMAGE_DIR/$NAME-seed.iso"
if [[ $DRY_RUN -eq 0 && -e "$DISK" ]]; then err "$DISK already exists"; exit 5; fi
info "creating overlay $DISK (${DISK_GB}G, backing $BASE_IMG)"
run qemu-img create -q -f qcow2 -F qcow2 -b "$BASE_IMG" "$DISK" "${DISK_GB}G"
CREATED_DISK="$DISK"

# ---------- cloud-init ----------
UD="$WORKDIR/user-data"; MD="$WORKDIR/meta-data"; NC="$WORKDIR/network-config"

printf 'instance-id: %s-%s\nlocal-hostname: %s\n' "$NAME" "$(date +%s)" "$HOSTNAME_FINAL" >"$MD"

if [[ -n "$USER_DATA_FILE" ]]; then
  cp "$USER_DATA_FILE" "$UD"
else
  {
    echo "#cloud-config"
    echo "hostname: $HOSTNAME_FINAL"
    echo "manage_etc_hosts: true"
    echo "users:"
    echo "  - name: $USERNAME"
    echo "    groups: [sudo]"
    echo "    shell: /bin/bash"
    echo "    sudo: ['ALL=(ALL) NOPASSWD:ALL']"
    echo "    lock_passwd: $([[ -n "$PASSWORD_HASH" ]] && echo false || echo true)"
    [[ -n "$PASSWORD_HASH" ]] && echo "    passwd: '$PASSWORD_HASH'"
    if [[ ${#AUTH_KEYS[@]} -gt 0 ]]; then
      echo "    ssh_authorized_keys:"
      for k in "${AUTH_KEYS[@]}"; do echo "      - '$k'"; done
    fi
    echo "ssh_pwauth: $([[ -n "$PASSWORD_HASH" ]] && echo true || echo false)"
    echo "package_update: true"
    echo "packages:"
    echo "  - qemu-guest-agent"
    if [[ -n "$PACKAGES" ]]; then IFS=',' read -ra pk <<<"$PACKAGES"; for p in "${pk[@]}"; do echo "  - $p"; done; fi
    echo "runcmd:"
    echo "  - [systemctl, enable, --now, qemu-guest-agent]"
    for c in "${RUNCMDS[@]:-}"; do [[ -n "$c" ]] && echo "  - $c"; done
    echo "final_message: 'cloud-init finished after \$UPTIME seconds'"
  } >"$UD"
fi

{
  echo "version: 2"
  echo "ethernets:"
  echo "  id0:"
  echo "    match: {name: 'en*'}"
  echo "    set-name: eth0"
  if [[ -n "$IP_CIDR" ]]; then
    echo "    dhcp4: false"
    echo "    addresses: [$IP_CIDR]"
    echo "    routes: [{to: default, via: $GATEWAY}]"
    echo "    nameservers: {addresses: [${DNS}]}"
  else
    echo "    dhcp4: true"
  fi
} >"$NC"

if [[ $DRY_RUN -eq 1 ]]; then
  info "generated user-data:"; sed 's/^/    /' "$UD" >&2
  info "generated network-config:"; sed 's/^/    /' "$NC" >&2
fi

info "building seed ISO with $SEED_TOOL"
case "$SEED_TOOL" in
  cloud-localds) run cloud-localds -N "$NC" "$SEED" "$UD" "$MD" ;;
  genisoimage|xorrisofs) run "$SEED_TOOL" -output "$SEED" -volid cidata -joliet -rock -quiet "$UD" "$MD" "$NC" ;;
esac
[[ $DRY_RUN -eq 0 ]] && chmod 0600 "$SEED"

# ---------- virt-install ----------
if [[ -n "$NETWORK" ]]; then NETOPT="network=$NETWORK,model=virtio"; else NETOPT="bridge=$BRIDGE,model=virtio"; fi
# shellcheck disable=SC2054  # commas are inside quoted virt-install option values
VI=(virt-install --connect "$CONNECT" --name "$NAME" --memory "$RAM_MB" --vcpus "$VCPUS"
    --cpu host-passthrough --os-variant "$OS_VARIANT" --import
    --disk "path=$DISK,format=qcow2,bus=virtio,discard=unmap"
    --disk "path=$SEED,device=cdrom,readonly=on"
    --network "$NETOPT" --graphics none --console pty,target_type=serial
    --channel unix,target_type=virtio,name=org.qemu.guest_agent.0
    --rng /dev/urandom --noautoconsole)
[[ $START -eq 0 ]] && VI+=(--print-xml)

info "defining and starting domain $NAME"
if [[ $START -eq 0 ]]; then
  if [[ $DRY_RUN -eq 1 ]]; then run "${VI[@]}"; else "${VI[@]}" >"$WORKDIR/dom.xml" && "${VIRSH[@]}" define "$WORKDIR/dom.xml" || exit 4; fi
  ok "domain $NAME defined (not started)"
  [[ $JSON -eq 1 ]] && printf '{"name":"%s","disk":"%s","seed":"%s","state":"defined","ip":null}\n' "$NAME" "$DISK" "$SEED"
  CREATED_DISK=""; exit 0
fi
run "${VI[@]}" || { err "virt-install failed"; exit 4; }
CREATED_DISK=""   # from here on the disk belongs to the domain

# ---------- wait for IP ----------
IP=""
if [[ $NO_WAIT -eq 0 && $DRY_RUN -eq 0 ]]; then
  info "waiting up to ${WAIT_SECS}s for an IP address"
  deadline=$((SECONDS + WAIT_SECS))
  while [[ $SECONDS -lt $deadline ]]; do
    IP=$("${VIRSH[@]}" domifaddr "$NAME" --source agent 2>/dev/null | awk '/ipv4/ && !/127\.0\.0\.1/ {split($4,a,"/"); print a[1]; exit}')
    [[ -z "$IP" ]] && IP=$("${VIRSH[@]}" domifaddr "$NAME" --source lease 2>/dev/null | awk '/ipv4/ {split($4,a,"/"); print a[1]; exit}')
    [[ -z "$IP" && -n "$IP_CIDR" ]] && { if "${VIRSH[@]}" qemu-agent-command "$NAME" '{"execute":"guest-ping"}' >/dev/null 2>&1; then IP="${IP_CIDR%/*}"; fi; }
    [[ -n "$IP" ]] && break
    sleep 3
  done
elif [[ $DRY_RUN -eq 1 ]]; then
  IP="${IP_CIDR%/*}"; IP="${IP:-<dhcp>}"
fi

if [[ -z "$IP" && $NO_WAIT -eq 0 && $DRY_RUN -eq 0 ]]; then
  warn "no IP after ${WAIT_SECS}s; VM is running - check: virsh console $NAME"
  [[ $JSON -eq 1 ]] && printf '{"name":"%s","disk":"%s","state":"running","ip":null}\n' "$NAME" "$DISK"
  exit 6
fi

ok "VM $NAME is up${IP:+ at $IP}"
if [[ $JSON -eq 1 ]]; then
  printf '{"name":"%s","release":"%s","vcpus":%s,"ram_mb":%s,"disk":"%s","ip":%s,"user":"%s","ssh":"ssh %s@%s"}\n' \
    "$NAME" "$RELEASE" "$VCPUS" "$RAM_MB" "$DISK" "$([[ -n "$IP" ]] && printf '"%s"' "$IP" || echo null)" "$USERNAME" "$USERNAME" "${IP:-$NAME}"
else
  printf '\n  ssh %s@%s\n\n' "$USERNAME" "${IP:-$NAME}"
fi
exit 0
