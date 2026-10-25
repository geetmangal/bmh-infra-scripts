"""Integration test for kvm/cloud-init-vm.sh base-image caching, using stub binaries.

Regression: the weekly refresh used to overwrite the cached base image in place,
corrupting every existing VM whose qcow2 overlay backs onto it.  Base images must be
versioned and immutable; new VMs use the newest one.
"""
import hashlib
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "kvm" / "cloud-init-vm.sh"
IMG = "noble-server-cloudimg-amd64.img"

STUBS = {
    # curl stub: copy $FAKE_MIRROR/<basename of URL> to the -o target
    "curl": r'''#!/bin/bash
out=""; url=""
while [[ $# -gt 0 ]]; do case "$1" in -o) out="$2"; shift 2;; --retry|--max-time) shift 2;; -*) shift;; *) url="$1"; shift;; esac; done
src="$FAKE_MIRROR/$(basename "$url")"; [[ -f "$src" ]] || exit 22
cp "$src" "$out"
''',
    "qemu-img": '#!/bin/bash\necho "$@" >> "$STUB_LOG"\nfor a in "$@"; do last="$a"; done\n',
    "virt-install": "#!/bin/bash\nexit 0\n",
    "virsh": '#!/bin/bash\n[[ " $* " == *" dominfo "* ]] && exit 1\nexit 0\n',
    "cloud-localds": '#!/bin/bash\n[[ "$1" == "-N" ]] && shift 2\ntouch "$1"\n',
}


@unittest.skipUnless(shutil.which("bash") and shutil.which("sha256sum"), "needs bash + coreutils")
class ImageCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.bin, self.mirror = self.tmp / "bin", self.tmp / "mirror"
        self.cache, self.images = self.tmp / "cache", self.tmp / "images"
        for d in (self.bin, self.mirror, self.images):
            d.mkdir()
        for name, body in STUBS.items():
            f = self.bin / name
            f.write_text(body)
            f.chmod(0o755)
        self.log = self.tmp / "stub.log"
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", FAKE_MIRROR=str(self.mirror),
                        STUB_LOG=str(self.log), UBUNTU_CLOUD_MIRROR="http://mirror.invalid", NO_COLOR="1")

    def tearDown(self):
        for p in self.tmp.rglob("*"):
            try:
                p.chmod(0o755)
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def publish(self, payload: bytes) -> str:
        (self.mirror / IMG).write_bytes(payload)
        sha = hashlib.sha256(payload).hexdigest()
        (self.mirror / "SHA256SUMS").write_text(f"{sha} *{IMG}\n")
        return sha

    def create(self, name):
        return subprocess.run(
            ["bash", str(SCRIPT), "--name", name, "--release", "24.04", "--cache-dir", str(self.cache),
             "--image-dir", str(self.images), "--no-wait", "--ssh-key", "ssh-ed25519 AAAAtest test@example"],
            check=False, env=self.env, capture_output=True, text=True, timeout=60)

    def backing_files(self):
        return [line.split(" -b ")[1].split()[0] for line in self.log.read_text().splitlines() if " -b " in line]

    def test_refresh_never_overwrites_existing_base(self):
        sha1 = self.publish(b"image-v1" * 1000)
        r = self.create("vm1")
        self.assertEqual(r.returncode, 0, r.stderr)
        base1 = self.cache / f"noble-server-cloudimg-amd64-{sha1[:12]}.img"
        self.assertTrue(base1.is_file())
        self.assertEqual(self.backing_files(), [str(base1)])

        # upstream publishes a new image; pointer is older than a week
        sha2 = self.publish(b"image-v2" * 1000)
        old = time.time() - 10 * 86400
        os.utime(self.cache / "noble-server-cloudimg-amd64.current", (old, old))
        r = self.create("vm2")
        self.assertEqual(r.returncode, 0, r.stderr)
        base2 = self.cache / f"noble-server-cloudimg-amd64-{sha2[:12]}.img"
        self.assertTrue(base2.is_file())
        # the base vm1 backs onto is untouched
        self.assertEqual(hashlib.sha256(base1.read_bytes()).hexdigest(), sha1)
        self.assertEqual(self.backing_files(), [str(base1), str(base2)])

    def test_fresh_pointer_reuses_cache_without_download(self):
        sha1 = self.publish(b"image-v1" * 1000)
        self.assertEqual(self.create("vm1").returncode, 0)
        self.publish(b"image-v2" * 1000)  # newer upstream, but pointer is fresh
        r = self.create("vm2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(list(self.cache.glob("*.img"))), 1)
        self.assertIn(sha1[:12], self.backing_files()[-1])

    def test_checksum_mismatch_keeps_nothing(self):
        self.publish(b"image-v1" * 1000)
        (self.mirror / "SHA256SUMS").write_text(f"{'0' * 64} *{IMG}\n")
        r = self.create("vm1")
        self.assertEqual(r.returncode, 3)
        self.assertEqual(list(self.cache.glob("*.img*")), [])


if __name__ == "__main__":
    unittest.main()
