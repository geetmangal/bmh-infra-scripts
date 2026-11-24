"""Unit tests for the pure leak-decision logic in ipoe-leak-reaper.py.

These exercise compute_leaks() without any netlink/kernel access, so they run
anywhere. The module filename is hyphenated, so load it by path.
"""
import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_PATH = os.path.join(_HERE, os.pardir, "bng", "ipoe-leak-reaper.py")
_spec = importlib.util.spec_from_file_location("ipoe_leak_reaper", _MOD_PATH)
reaper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reaper)

compute_leaks = reaper.compute_leaks


def test_no_leaks_when_module_subset_of_daemon():
    leaks, reapable, state = compute_leaks({1, 2, 3}, {1, 2, 3, 4}, {}, now=1000.0, min_age=60)
    assert leaks == set()
    assert reapable == set()
    assert state == {}


def test_fresh_leak_is_not_reapable_and_starts_clock():
    # ifindex 9 is in module but not daemon, first time seen -> within age guard
    leaks, reapable, state = compute_leaks({9}, set(), {}, now=1000.0, min_age=60)
    assert leaks == {9}
    assert reapable == set()
    assert state == {"9": 1000.0}  # clock started now


def test_persisted_leak_becomes_reapable_after_min_age():
    prev = {"9": 1000.0}  # first seen 1000
    leaks, reapable, state = compute_leaks({9}, set(), prev, now=1070.0, min_age=60)
    assert leaks == {9}
    assert reapable == {9}            # 70s >= 60s
    assert state == {"9": 1000.0}     # first_seen carried forward, not reset


def test_leak_just_under_min_age_still_guarded():
    prev = {"9": 1000.0}
    leaks, reapable, _ = compute_leaks({9}, set(), prev, now=1059.0, min_age=60)
    assert leaks == {9}
    assert reapable == set()          # 59s < 60s


def test_daemon_extra_ifindexes_are_ignored():
    # daemon knows sessions the module doesn't (shouldn't happen, but be safe)
    leaks, reapable, _ = compute_leaks({1}, {1, 2, 3}, {}, now=5.0, min_age=0)
    assert leaks == set()
    assert reapable == set()


def test_min_age_zero_reaps_immediately():
    leaks, reapable, _ = compute_leaks({7}, set(), {}, now=5.0, min_age=0)
    assert leaks == {7}
    assert reapable == {7}


def test_cleared_leak_drops_from_state():
    # ifindex 9 was a tracked leak; now it's back in the daemon -> not a leak,
    # and must not linger in the new state.
    prev = {"9": 1000.0}
    leaks, reapable, state = compute_leaks({9}, {9}, prev, now=2000.0, min_age=60)
    assert leaks == set()
    assert "9" not in state


def test_multiple_leaks_mixed_ages():
    prev = {"10": 100.0}           # old leak
    leaks, reapable, state = compute_leaks(
        {10, 20}, set(), prev, now=200.0, min_age=60)
    assert leaks == {10, 20}
    assert reapable == {10}        # 10 is old enough; 20 is brand new
    assert state["10"] == 100.0
    assert state["20"] == 200.0
