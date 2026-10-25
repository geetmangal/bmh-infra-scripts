"""Unit tests for bgp/anycast-healthcheck.py (hysteresis, config parsing, controller with a fake backend)."""
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("anycast", ROOT / "bgp" / "anycast-healthcheck.py")
anycast = importlib.util.module_from_spec(SPEC)
sys.modules["anycast"] = anycast
SPEC.loader.exec_module(anycast)  # type: ignore[union-attr]


class FakeBackend(anycast.Backend):
    name = "fake"

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def announce(self):
        self.calls.append("announce")
        if self.fail:
            raise anycast.BackendError("boom")

    def withdraw(self):
        self.calls.append("withdraw")
        if self.fail:
            raise anycast.BackendError("boom")


class HysteresisTests(unittest.TestCase):
    def test_withdraw_after_n_failures(self):
        fsm = anycast.Hysteresis(3, 2)
        self.assertIsNone(fsm.feed(False))
        self.assertIsNone(fsm.feed(False))
        self.assertEqual(fsm.feed(False), "withdraw")
        self.assertFalse(fsm.announced)
        self.assertIsNone(fsm.feed(False), "no repeated withdraw")

    def test_flap_resets_counter(self):
        fsm = anycast.Hysteresis(3, 2)
        fsm.feed(False)
        fsm.feed(False)
        fsm.feed(True)
        fsm.feed(False)
        self.assertIsNone(fsm.feed(False))
        self.assertTrue(fsm.announced)

    def test_announce_after_m_successes(self):
        fsm = anycast.Hysteresis(1, 3, initial=anycast.Hysteresis.WITHDRAWN)
        self.assertIsNone(fsm.feed(True))
        self.assertIsNone(fsm.feed(True))
        self.assertEqual(fsm.feed(True), "announce")
        self.assertTrue(fsm.announced)

    def test_thresholds_validated(self):
        with self.assertRaises(ValueError):
            anycast.Hysteresis(0, 1)

    def test_roundtrip_dict(self):
        fsm = anycast.Hysteresis(2, 2)
        fsm.feed(False)
        d = fsm.to_dict()
        fsm2 = anycast.Hysteresis(2, 2)
        fsm2.load(d)
        self.assertEqual(fsm2.consecutive_fail, 1)
        self.assertEqual(fsm2.feed(False), "withdraw")


class ConfigTests(unittest.TestCase):
    YAML = """
# comment
prefix: 203.0.113.53/32
interval: 2
fail_threshold: 3
ok_threshold: 5
backend: bird
bird_protocol: anycast_static
checks:
  - name: dns
    type: dns
    host: 127.0.0.1
    qname: example.com
  - name: web
    type: http
    url: http://127.0.0.1:8080/healthz
    expect_status: [200, 204]
    body_regex: 'ok'
    verify_tls: false
  - name: tcp
    type: tcp
    port: 853
"""

    def _write(self, text, suffix=".yaml"):
        fd, path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def test_yaml_subset(self):
        cfg = anycast.load_config(self._write(self.YAML))
        self.assertEqual(cfg.prefix, "203.0.113.53/32")
        self.assertEqual(cfg.interval, 2)
        self.assertEqual(cfg.fail_threshold, 3)
        self.assertEqual(len(cfg.checks), 3)
        dns, web, tcp = cfg.checks
        self.assertEqual(dns.type, "dns")
        self.assertEqual(dns.port, 53)
        self.assertEqual(web.expect_status, [200, 204])
        self.assertFalse(web.verify_tls)
        self.assertEqual(web.body_regex, "ok")
        self.assertEqual(tcp.port, 853)

    def test_json(self):
        data = {"prefix": "2001:db8::53/128", "backend": "exec", "exec_up": "true", "exec_down": "true",
                "checks": [{"name": "t", "type": "tcp", "port": 22}]}
        cfg = anycast.load_config(self._write(json.dumps(data), ".json"))
        self.assertEqual(cfg.backend, "exec")
        self.assertEqual(cfg.checks[0].port, 22)

    def test_example_config_parses(self):
        cfg = anycast.load_config(self._write(anycast.EXAMPLE_CONFIG))
        self.assertEqual(len(cfg.checks), 3)
        self.assertEqual(cfg.checks[1].expect_status, [200, 204])

    def test_errors(self):
        with self.assertRaises(anycast.ConfigError):
            anycast.config_from_dict({"checks": []})
        with self.assertRaises(anycast.ConfigError):
            anycast.config_from_dict({"bogus": 1, "checks": [{"type": "tcp", "port": 1}]})
        with self.assertRaises(anycast.ConfigError):
            anycast.config_from_dict({"checks": [{"type": "http"}]})
        with self.assertRaises(anycast.ConfigError):
            anycast.config_from_dict({"checks": [{"type": "tcp", "port": 70000}]})
        with self.assertRaises(anycast.ConfigError):
            anycast.config_from_dict({"backend": "vyos", "checks": [{"type": "tcp", "port": 1}]})
        with self.assertRaises(anycast.ConfigError):
            anycast.config_from_dict({"checks": [{"type": "http", "url": "http://x", "body_regex": "("}]})


class DnsPacketTests(unittest.TestCase):
    def test_build_and_parse(self):
        pkt = anycast.build_dns_query("example.com", "A", 0x1234)
        self.assertEqual(pkt[:2], b"\x12\x34")
        self.assertIn(b"\x07example\x03com\x00", pkt)
        reply = b"\x12\x34\x81\x80" + b"\x00\x01\x00\x02\x00\x00\x00\x00" + pkt[12:]
        rcode, an = anycast.parse_dns_response(reply, 0x1234)
        self.assertEqual((rcode, an), (0, 2))
        with self.assertRaises(ValueError):
            anycast.parse_dns_response(reply, 0x9999)


class ControllerTests(unittest.TestCase):
    def _cfg(self, **kw):
        base = {"prefix": "203.0.113.1/32", "fail_threshold": 2, "ok_threshold": 2, "state_file": "", "backend": "exec",
                "exec_up": "true", "exec_down": "true", "checks": [{"name": "dead", "type": "tcp", "host": "127.0.0.1", "port": 1, "timeout": 0.3}]}
        base.update(kw)
        return anycast.config_from_dict(base)

    def test_withdraw_then_announce_with_fake_backend(self):
        cfg = self._cfg()
        fb = FakeBackend()
        ctl = anycast.Controller(cfg, fb)
        healthy, t = ctl.round()
        self.assertFalse(healthy)
        self.assertIsNone(t)
        healthy, t = ctl.round()
        self.assertEqual(t, "withdraw")
        self.assertEqual(fb.calls, ["withdraw"])
        # fake the checks passing
        anycast.CHECKERS["tcp"] = lambda spec: anycast.CheckResult(spec.name, True, 0.1, "fake")
        try:
            ctl.round()
            _, t = ctl.round()
        finally:
            anycast.CHECKERS["tcp"] = anycast.check_tcp
        self.assertEqual(t, "announce")
        self.assertEqual(fb.calls, ["withdraw", "announce"])
        s = ctl.summary(True, t)
        self.assertTrue(s["announced"])
        self.assertEqual(s["checks"][0]["name"], "dead")

    def test_backend_error_counted(self):
        ctl = anycast.Controller(self._cfg(fail_threshold=1), FakeBackend(fail=True))
        ctl.round()
        self.assertEqual(ctl.backend_errors, 1)
        self.assertFalse(ctl.fsm.announced, "state flips even if backend failed, so the next round retries nothing twice")

    def test_state_file_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            sf = os.path.join(d, "state.json")
            cfg = self._cfg(state_file=sf, fail_threshold=1)
            ctl = anycast.Controller(cfg, FakeBackend())
            ctl.round()
            self.assertTrue(os.path.exists(sf))
            ctl2 = anycast.Controller(cfg, FakeBackend())
            self.assertFalse(ctl2.fsm.announced)


if __name__ == "__main__":
    unittest.main()
