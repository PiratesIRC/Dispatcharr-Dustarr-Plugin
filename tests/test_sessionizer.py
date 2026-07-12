import pytest
from conftest import load_pure

THRESHOLDS = {"min_watch_seconds": 120.0, "client_gap_grace_s": 90.0,
              "merge_gap_s": 120.0, "poll_interval_s": 15.0}


@pytest.fixture()
def sess_mod():
    return load_pure("sessionizer")


@pytest.fixture()
def sess(sess_mod):
    return sess_mod.Sessionizer(dict(THRESHOLDS))


def poll(sess, t, present, interval=15.0):
    sess.observe(t, present, interval)


def test_qualified_watch_recorded_after_threshold(sess):
    t = 1000.0
    for i in range(11):                       # 10 intervals x 15s = 150s >= 120s
        poll(sess, t + i * 15, {"u1": 1})
    poll(sess, t + 400, {})                   # vanish + merge window elapsed
    rec = sess.channels["u1"]
    assert rec["watch_count"] == 1
    assert rec["watch_seconds"] == pytest.approx(150.0)
    assert rec["tune_count"] == 1


def test_channel_surf_below_threshold_is_not_a_watch_but_updates_last_tuned(sess):
    t = 1000.0
    poll(sess, t, {"u1": 1})
    poll(sess, t + 15, {"u1": 1})             # 15s only
    poll(sess, t + 400, {})
    rec = sess.channels["u1"]
    assert rec["watch_count"] == 0
    assert rec["watch_seconds"] == 0
    assert rec["tune_count"] == 1             # it WAS tuned -- the report needs this
    assert rec["last_tuned"] == 1000.0
    assert rec["last_watched"] is None


def test_merged_session_counts_once_not_twice(sess):
    """A 150s segment + a reopen + a 300s segment is ONE watch of 450s.

    Accounting at provisional close would double-count (consistency #7).
    """
    t = 1000.0
    for i in range(11):                       # 150s
        poll(sess, t + i * 15, {"u1": 1})
    poll(sess, t + 160, {"u1": 0})            # clients drop; provisional close
    poll(sess, t + 220, {"u1": 1})            # reopen inside merge_gap_s
    for i in range(1, 21):                    # +300s
        poll(sess, t + 220 + i * 15, {"u1": 1})
    poll(sess, t + 900, {})                   # gone; merge window elapsed
    rec = sess.channels["u1"]
    assert rec["watch_count"] == 1
    assert rec["watch_seconds"] == pytest.approx(450.0)


def test_zero_client_tail_credits_nothing(sess):
    t = 1000.0
    for i in range(11):                       # 150s of real watching
        poll(sess, t + i * 15, {"u1": 1})
    for i in range(1, 8):                     # 105s at zero clients
        poll(sess, t + 150 + i * 15, {"u1": 0})
    poll(sess, t + 600, {})
    # Only the clients>=1 polls are credited; the grace tail is not watch time.
    assert sess.channels["u1"]["watch_seconds"] == pytest.approx(150.0)


def test_grace_survives_a_player_retry_gap(sess):
    """A 60s retry gap must NOT split the watch (dmonitarr E1: gaps exceed 40s)."""
    t = 1000.0
    for i in range(5):
        poll(sess, t + i * 15, {"u1": 1})     # 60s
    for i in range(1, 5):
        poll(sess, t + 60 + i * 15, {"u1": 0})  # 60s of zero clients < 90s grace
    for i in range(1, 9):
        poll(sess, t + 120 + i * 15, {"u1": 1})  # back, +120s
    poll(sess, t + 600, {})
    rec = sess.channels["u1"]
    assert rec["watch_count"] == 1            # ONE session, not two
    assert rec["watch_seconds"] == pytest.approx(180.0)


def test_grace_floor_survives_throttled_interval(sess):
    """Under throttling effective_interval=120s > the 90s grace. A bare 90s grace
    would close the session on the FIRST zero-client poll (consistency #6)."""
    t = 1000.0
    poll(sess, t, {"u1": 1}, interval=120.0)
    poll(sess, t + 120, {"u1": 1}, interval=120.0)
    poll(sess, t + 240, {"u1": 0}, interval=120.0)   # one zero poll
    assert "u1" in sess.open_sessions                # still open, not closed
    poll(sess, t + 360, {"u1": 1}, interval=120.0)   # recovers
    assert sess.channels["u1"]["watch_count"] == 0   # not finalized yet
    assert "u1" in sess.open_sessions


def test_clock_jump_forward_is_clamped(sess):
    """Host sleep / NTP step must not credit hours of phantom watch time."""
    t = 1000.0
    poll(sess, t, {"u1": 1})
    poll(sess, t + 7200, {"u1": 1})           # 2h jump in one poll
    poll(sess, t + 7800, {})
    # Credit is capped at 2 x effective_interval per poll.
    assert sess.channels["u1"]["watch_seconds"] == pytest.approx(30.0)


def test_backward_clock_step_clamps_to_zero(sess):
    t = 1000.0
    poll(sess, t, {"u1": 1})
    poll(sess, t - 50, {"u1": 1})             # clock went backwards
    assert sess.open_sessions["u1"].accumulated >= 0


def test_multiple_clients_is_wall_clock_not_client_seconds(sess):
    t = 1000.0
    for i in range(11):
        poll(sess, t + i * 15, {"u1": 3})     # three viewers
    poll(sess, t + 400, {})
    # 150s of channel wall-clock, NOT 450s. Two clients != 2x the channel's value.
    assert sess.channels["u1"]["watch_seconds"] == pytest.approx(150.0)


def test_stream_switch_mid_session_is_one_continuous_session(sess):
    """The metadata hash is rewritten in place on failover; keying on the channel
    UUID means the session must survive it untouched."""
    t = 1000.0
    for i in range(11):
        poll(sess, t + i * 15, {"u1": 1})
    poll(sess, t + 400, {})
    assert sess.channels["u1"]["watch_count"] == 1


def test_waiting_for_clients_proxy_never_opens_a_session(sess):
    t = 1000.0
    for i in range(20):
        poll(sess, t + i * 15, {"u1": 0})     # metadata present, no clients ever
    assert "u1" not in sess.channels
    assert "u1" not in sess.open_sessions


def test_metadata_key_blink_does_not_hard_close(sess):
    """The 30s TTL is refreshed per-second only for channels the worker owns; the
    key can legitimately blink around a switch. One missing poll must fall through
    to the grace path, not close the session (fact-check #3)."""
    t = 1000.0
    for i in range(5):
        poll(sess, t + i * 15, {"u1": 1})
    poll(sess, t + 60, {})                    # key gone for ONE poll
    poll(sess, t + 75, {"u1": 1})             # back
    assert sess.open_sessions["u1"].accumulated > 0
    assert sess.channels.get("u1", {}).get("watch_count", 0) == 0  # not finalized


def test_first_seen_is_set_on_first_observation(sess):
    poll(sess, 1000.0, {"u1": 1})
    assert sess.channels["u1"]["first_seen"] == 1000.0
    poll(sess, 2000.0, {"u1": 1})
    assert sess.channels["u1"]["first_seen"] == 1000.0  # never moves


def test_reset_sessions_drops_open_sessions_without_crediting(sess):
    """Leader change: open sessions are DROPPED, not adopted (product #1)."""
    t = 1000.0
    for i in range(11):
        poll(sess, t + i * 15, {"u1": 1})
    sess.reset_sessions()
    assert sess.open_sessions == {}
    assert sess.channels["u1"]["watch_count"] == 0   # in-flight time is forfeited


def test_coverage_counts_ticks_and_max_gap(sess):
    t = 1_700_000_000.0
    poll(sess, t, {})
    poll(sess, t + 15, {})
    poll(sess, t + 100, {})                   # an 85s gap
    bucket = sess.coverage[sess_bucket_key(t)]
    assert bucket["ticks"] == 3
    assert bucket["max_gap_s"] == pytest.approx(85.0)


def sess_bucket_key(ts):
    import time
    return time.strftime("%Y-%m-%dT%H", time.gmtime(ts))


def test_prune_coverage_drops_old_buckets(sess):
    t = 1_700_000_000.0
    poll(sess, t, {})
    old = t + 60 * 86400
    poll(sess, old, {})
    sess.prune_coverage(old, keep_days=45)
    assert sess_bucket_key(t) not in sess.coverage
    assert sess_bucket_key(old) in sess.coverage
