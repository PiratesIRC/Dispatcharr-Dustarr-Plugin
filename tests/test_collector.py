import pytest
from conftest import FakeClock, FakeRedis, load_pure

THRESHOLDS = {"min_watch_seconds": 120.0, "client_gap_grace_s": 90.0,
              "merge_gap_s": 120.0, "poll_interval_s": 15.0}


@pytest.fixture()
def col_mod():
    return load_pure("collector")


@pytest.fixture()
def sess_mod():
    return load_pure("sessionizer")


@pytest.fixture()
def storage_mod():
    return load_pure("storage")


def make_collector(col_mod, sess_mod, storage_mod, redis, clock, tmp_path,
                   token="pid1:abc"):
    sess = sess_mod.Sessionizer(dict(THRESHOLDS))
    store = storage_mod.Storage(str(tmp_path))
    return col_mod.Collector(redis, sess, store, dict(THRESHOLDS),
                             token=token, wall=clock.wall)


def test_lease_acquired_when_free(col_mod, fake_redis, fake_clock):
    lease = col_mod.Lease(fake_redis, "me")
    assert lease.tick(fake_clock.wall()) is True
    assert lease.owned is True


def test_lease_never_steals_a_foreign_lease(col_mod, fake_redis, fake_clock):
    col_mod.Lease(fake_redis, "other").tick(fake_clock.wall())
    mine = col_mod.Lease(fake_redis, "me")
    assert mine.tick(fake_clock.wall()) is False


def test_lease_renews_its_own(col_mod, fake_redis, fake_clock):
    lease = col_mod.Lease(fake_redis, "me")
    lease.tick(fake_clock.wall())
    fake_clock.advance(30)
    assert lease.tick(fake_clock.wall()) is True


def test_lease_tick_acquired_fresh_only_on_free_key_acquisition(col_mod, fake_redis,
                                                                 fake_clock):
    """acquired_fresh distinguishes a genuine leadership TRANSITION (SET NX on
    a free key) from a plain renewal of a key we already held -- FIX2 keys
    the reload-on-reacquire decision off this, not off the caller's own
    (possibly stale) belief."""
    lease = col_mod.Lease(fake_redis, "me")
    assert lease.tick(fake_clock.wall()) is True
    assert lease.acquired_fresh is True

    fake_clock.advance(10)
    assert lease.tick(fake_clock.wall()) is True    # renewal of our own key
    assert lease.acquired_fresh is False


def test_lease_renew_never_acquires_a_free_key(col_mod, fake_redis, fake_clock):
    """renew() is the write-fence primitive (FIX1): unlike tick(), it must
    NEVER SET NX a free key -- a follower calling renew() on a free lease
    must stay a follower, full stop."""
    lease = col_mod.Lease(fake_redis, "me")
    assert lease.renew(fake_clock.wall()) is False
    assert fake_redis.get(col_mod.LEADER_KEY) is None
    assert lease.owned is False


def test_lease_renew_renews_a_key_it_already_owns(col_mod, fake_redis, fake_clock):
    lease = col_mod.Lease(fake_redis, "me")
    lease.tick(fake_clock.wall())
    fake_clock.advance(30)
    assert lease.renew(fake_clock.wall()) is True


def test_lease_get_error_is_counted_and_leaves_owned_unchanged(col_mod, fake_redis,
                                                                fake_clock):
    """FIX4: a transient Redis blip on the lease path must be counted, not
    silently swallowed -- and must not flip a confirmed leader to deposed
    (deposition should require an OBSERVED foreign token, not an I/O error)."""
    class FlakyOnce(FakeRedis):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.fail_next_get = False

        def get(self, key):
            if self.fail_next_get:
                self.fail_next_get = False
                raise RuntimeError("redis blip")
            return super().get(key)

    redis = FlakyOnce(clock=fake_clock)
    lease = col_mod.Lease(redis, "me")
    assert lease.tick(fake_clock.wall()) is True
    assert lease.owned is True

    redis.fail_next_get = True
    fake_clock.advance(10)
    assert lease.tick(fake_clock.wall()) is True    # unchanged, not deposed
    assert lease.last_error is not None


def test_follower_never_writes_usage_json(col_mod, sess_mod, storage_mod,
                                          fake_redis, fake_clock, tmp_path):
    # The other leader must keep renewing every tick, exactly like a real
    # collector would -- LEASE_TTL is 60s and this loop spans 150s, so a
    # single unrenewed .tick() would legitimately lapse partway through and
    # "me" would rightfully take over (correct behavior, not a bug). Renewing
    # here is what actually exercises "a live foreign leader is never usurped".
    leader = col_mod.Lease(fake_redis, "the-leader")
    leader.tick(fake_clock.wall())
    col = make_collector(col_mod, sess_mod, storage_mod, fake_redis, fake_clock,
                         tmp_path, token="me")
    fake_redis.open_channel("u1", clients=1)
    for _ in range(10):
        leader.tick(fake_clock.wall())
        col.run_tick()
        fake_clock.advance(15)
    assert not (tmp_path / "usage.json").exists()
    # Not just "no file" -- assert the actual mechanism: this worker never
    # believed itself leader, and the foreign lease is still exactly what it
    # was, so the negative assertion above can't pass for the wrong reason.
    assert col._was_leader is False
    assert fake_redis.get(col_mod.LEADER_KEY) == "the-leader"


def test_leader_samples_and_flushes(col_mod, sess_mod, storage_mod, fake_redis,
                                    fake_clock, tmp_path):
    col = make_collector(col_mod, sess_mod, storage_mod, fake_redis, fake_clock,
                         tmp_path)
    for _ in range(12):                        # 180s of watching
        # open_channel's metadata TTL is a hard 30s with no auto-refresh (it
        # models Dispatcharr's real 30s TTL, refreshed by Dispatcharr every
        # second in production). Re-arm it every poll or the fake channel
        # "goes dark" after 30s and the session never accrues 180s.
        fake_redis.open_channel("u1", clients=1)
        col.run_tick()
        fake_clock.advance(15)
    fake_redis.close_channel("u1")
    for _ in range(20):                        # drain the grace + merge window
        col.run_tick()
        fake_clock.advance(15)
    data = storage_mod.Storage(str(tmp_path)).load(fake_clock.wall())
    assert data["channels"]["u1"]["watch_count"] == 1
    assert data["meta"]["stats_since"] > 0


def test_presence_set_comes_from_the_full_scan(col_mod, sess_mod, storage_mod,
                                               fake_redis, fake_clock, tmp_path):
    """Every present channel is SCARDed every cycle -- no round-robin cap. A cap
    can miss an entire 2-minute watch (red-team #13)."""
    col = make_collector(col_mod, sess_mod, storage_mod, fake_redis, fake_clock,
                         tmp_path)
    for i in range(40):
        fake_redis.open_channel(f"u{i}", clients=1)
    col.run_tick()
    assert col.stats["channels_seen"] == 40
    assert len(col.sessionizer.open_sessions) == 40


def test_losing_leadership_drops_open_sessions(col_mod, sess_mod, storage_mod,
                                               fake_redis, fake_clock, tmp_path):
    col = make_collector(col_mod, sess_mod, storage_mod, fake_redis, fake_clock,
                         tmp_path, token="me")
    fake_redis.open_channel("u1", clients=1)
    col.run_tick()
    assert col.sessionizer.open_sessions
    # Another worker steals the lease (ours expired while we were stalled).
    fake_redis.delete(col_mod.LEADER_KEY)
    col_mod.Lease(fake_redis, "other").tick(fake_clock.wall())
    fake_clock.advance(15)
    col.run_tick()
    assert col.sessionizer.open_sessions == {}


def test_redis_error_is_counted_not_raised(col_mod, sess_mod, storage_mod,
                                           fake_clock, tmp_path):
    class BrokenRedis(FakeRedis):
        def scan_iter(self, match=None, count=None):
            raise RuntimeError("redis is down")

    redis = BrokenRedis(clock=fake_clock)
    col = make_collector(col_mod, sess_mod, storage_mod, redis, fake_clock, tmp_path)
    col.run_tick()                             # must not raise
    assert col.stats["redis_errors"] == 1
    assert col.stats["last_error"]


def test_over_budget_cycles_throttle_the_interval(col_mod, sess_mod, storage_mod,
                                                  fake_redis, fake_clock, tmp_path,
                                                  monkeypatch):
    col = make_collector(col_mod, sess_mod, storage_mod, fake_redis, fake_clock,
                         tmp_path)
    # Force every cycle to look slow.
    monkeypatch.setattr(col, "_cycle_ms", lambda start: col_mod.HARD_BUDGET_MS + 1)
    for _ in range(4):
        col.run_tick()
        # Advance by at least effective_interval between calls so each call
        # actually samples under the FIX3 cadence gate (run_tick may now be
        # called more often than effective_interval without re-sampling).
        fake_clock.advance(col.effective_interval)
    assert col.effective_interval > THRESHOLDS["poll_interval_s"]
    assert col.effective_interval <= col_mod.THROTTLE_CEILING_S


def test_sustained_in_budget_cycles_untrottle_back_to_configured(
        col_mod, sess_mod, storage_mod, fake_redis, fake_clock, tmp_path, monkeypatch):
    """Mutation-testing found deleting the un-throttle branch left every
    existing test green (task review FIX5) -- nothing exercised the recovery
    path back down from a throttled interval."""
    col = make_collector(col_mod, sess_mod, storage_mod, fake_redis, fake_clock,
                         tmp_path)
    monkeypatch.setattr(col, "_cycle_ms", lambda start: col_mod.HARD_BUDGET_MS + 1)
    for _ in range(3):
        col.run_tick()
        fake_clock.advance(col.effective_interval)
    assert col.effective_interval > THRESHOLDS["poll_interval_s"]

    monkeypatch.setattr(col, "_cycle_ms", lambda start: col_mod.SOFT_BUDGET_MS - 1)
    for _ in range(col_mod.UNTHROTTLE_AFTER):
        col.run_tick()
        fake_clock.advance(col.effective_interval)
    assert col.effective_interval == THRESHOLDS["poll_interval_s"]


def test_base_tick_stays_lease_safe_while_throttled_to_the_ceiling(
        col_mod, sess_mod, storage_mod, fake_redis, fake_clock, tmp_path, monkeypatch):
    """FIX3: THROTTLE_CEILING_S (120) exceeds LEASE_TTL (60). If the external
    thread loop only wakes up every base_tick() seconds and base_tick() ==
    effective_interval, a throttled leader's lease lapses every cycle. Once
    fully throttled, base_tick() must return a cadence that keeps renewing
    inside LEASE_TTL."""
    col = make_collector(col_mod, sess_mod, storage_mod, fake_redis, fake_clock,
                         tmp_path)
    monkeypatch.setattr(col, "_cycle_ms", lambda start: col_mod.HARD_BUDGET_MS + 1)
    while col.effective_interval < col_mod.THROTTLE_CEILING_S:
        col.run_tick()
        fake_clock.advance(col.effective_interval)
    assert col.effective_interval == col_mod.THROTTLE_CEILING_S
    assert col.base_tick() <= col_mod.LEASE_TTL / 3.0


def test_throttled_leader_keeps_the_lease_against_a_competitor(
        col_mod, sess_mod, storage_mod, fake_redis, fake_clock, tmp_path):
    """Behavioral version of FIX3: drive run_tick on base_tick()'s own
    cadence (as the real thread loop would) while throttled to the ceiling,
    with a competing follower probing the lease halfway through every hop.
    Under the old cadence (base_tick() == effective_interval == 120s > the
    60s TTL) the competitor gets in during the gap; the fix must keep the
    lease continuously renewed so it never does."""
    col = make_collector(col_mod, sess_mod, storage_mod, fake_redis, fake_clock,
                         tmp_path, token="me")
    col.run_tick()   # establish leadership + a known-good renewal baseline
    # Jump straight to the ceiling: the throttling ramp itself is covered by
    # other tests, this test is only about lease safety once fully throttled.
    col.effective_interval = col_mod.THROTTLE_CEILING_S

    other = col_mod.Lease(fake_redis, "other")
    step = col.base_tick()
    hops = int((col_mod.THROTTLE_CEILING_S + col_mod.LEASE_TTL) / step) + 1
    for _ in range(hops):
        fake_clock.advance(step / 2.0)
        other.tick(fake_clock.wall())
        assert other.owned is False, "a competitor took the lease mid-cycle"
        fake_clock.advance(step / 2.0)
        col.run_tick()
    assert col.lease.owned is True


def test_self_health_is_written_to_meta(col_mod, sess_mod, storage_mod, fake_redis,
                                        fake_clock, tmp_path):
    col = make_collector(col_mod, sess_mod, storage_mod, fake_redis, fake_clock,
                         tmp_path)
    col.run_tick()
    fake_clock.advance(col_mod.FLUSH_INTERVAL_S + 1)
    col.run_tick()
    data = storage_mod.Storage(str(tmp_path)).load(fake_clock.wall())
    health = data["meta"]["self_health"]
    assert health["total_ticks"] == 2
    assert health["last_tick_ts"] > 0
    assert health["effective_interval"] == THRESHOLDS["poll_interval_s"]


def test_shutdown_releases_own_lease_but_never_a_foreign_one(col_mod, sess_mod,
                                                              storage_mod, fake_redis,
                                                              fake_clock, tmp_path):
    # Was misnamed/under-tested: only ever asserted its own key vanished,
    # never that a FOREIGN lease survives a different worker's shutdown --
    # the exact gap that let FIX1 (shutdown bypassing the write fence) slip
    # through (task review FIX5).
    col = make_collector(col_mod, sess_mod, storage_mod, fake_redis, fake_clock,
                         tmp_path, token="me")
    col.run_tick()
    col.shutdown()
    assert fake_redis.get(col_mod.LEADER_KEY) is None

    leader = col_mod.Lease(fake_redis, "the-leader")
    leader.tick(fake_clock.wall())
    follower = make_collector(col_mod, sess_mod, storage_mod, fake_redis, fake_clock,
                              tmp_path, token="follower")
    follower.shutdown()
    assert fake_redis.get(col_mod.LEADER_KEY) == "the-leader"
    assert follower._was_leader is False


def test_follower_shutdown_does_not_wipe_usage_json_when_lease_is_free(
        col_mod, sess_mod, storage_mod, fake_redis, fake_clock, tmp_path):
    """CRITICAL repro (task review FIX1): all uWSGI workers stop together, so
    by the time a FOLLOWER (one that never led) reaches shutdown() the real
    leader's key has often already lapsed and is free. The old shutdown()
    called _flush() unconditionally, and _flush()'s old fence was
    lease.tick() -- which SET-NXs a free key -- so the follower "became
    leader" just in time to overwrite a real recorded watch with its own
    never-loaded empty state."""
    store = storage_mod.Storage(str(tmp_path))
    store.write({"channels": {"u1": {"watch_count": 1, "watch_seconds": 500.0,
                                      "tune_count": 1, "last_watched": 100.0,
                                      "last_tuned": 100.0, "first_seen": 50.0}},
                 "meta": {"stats_since": 50.0}}, 100.0)

    col = make_collector(col_mod, sess_mod, storage_mod, fake_redis, fake_clock,
                         tmp_path, token="follower-never-led")
    assert fake_redis.get(col_mod.LEADER_KEY) is None  # the real leader's key already lapsed
    col.shutdown()

    data = storage_mod.Storage(str(tmp_path)).load(fake_clock.wall())
    assert data["channels"]["u1"]["watch_count"] == 1   # must NOT have been wiped
    assert fake_redis.get(col_mod.LEADER_KEY) is None   # never took the free lease either


def test_unobserved_lease_loss_does_not_clobber_interim_leaders_writes(
        col_mod, sess_mod, storage_mod, fake_redis, fake_clock, tmp_path):
    """CRITICAL repro (task review FIX2): "me" stalls past LEASE_TTL without
    ever running a tick, so its in-memory `_was_leader` never observes the
    loss. An interim leader takes over during the stall, records a real
    watch, and cleanly exits. When "me" resumes it re-acquires the now-free
    key via SET NX (a fresh acquisition) -- that must be treated as a
    leadership transition regardless of `_was_leader`, or "me" flushes its
    stale in-memory state over the interim leader's write."""
    col = make_collector(col_mod, sess_mod, storage_mod, fake_redis, fake_clock,
                         tmp_path, token="me")
    fake_redis.open_channel("u1", clients=1)
    col.run_tick()
    assert "u1" in col.sessionizer.open_sessions
    stale_session = col.sessionizer.open_sessions["u1"]

    # "me" stalls past LEASE_TTL without ever calling run_tick again --
    # _was_leader stays True the whole time; it never observes the loss.
    fake_clock.advance(col_mod.LEASE_TTL + 30)

    # An interim leader takes over during the stall (the key lapsed),
    # records a watch for a DIFFERENT channel, and writes it to disk.
    interim = col_mod.Lease(fake_redis, "interim")
    assert interim.tick(fake_clock.wall()) is True
    interim_store = storage_mod.Storage(str(tmp_path))
    existing = interim_store.ensure_stats_since(interim_store.load(fake_clock.wall()),
                                                 fake_clock.wall())
    existing["channels"]["u2"] = {"watch_count": 1, "watch_seconds": 500.0,
                                  "tune_count": 1, "last_watched": fake_clock.wall(),
                                  "last_tuned": fake_clock.wall(),
                                  "first_seen": fake_clock.wall()}
    assert interim_store.write(existing, fake_clock.wall())
    interim.release()   # frees the key cleanly

    # "me" resumes. Re-arm u1's metadata TTL and tick.
    fake_redis.open_channel("u1", clients=1)
    assert col._was_leader is True   # never observed the loss
    col.run_tick()

    # Reload must have happened: u2 (written by the interim leader, which
    # "me" never saw) must now be present in memory, and the stale in-flight
    # session from before the stall must have been dropped, not silently
    # adopted.
    assert "u2" in col.sessionizer.channels
    assert col.sessionizer.open_sessions.get("u1") is not stale_session

    data = storage_mod.Storage(str(tmp_path)).load(fake_clock.wall())
    assert data["channels"]["u2"]["watch_count"] == 1   # interim leader's write survives


def test_existing_usage_is_loaded_on_first_tick(col_mod, sess_mod, storage_mod,
                                                fake_redis, fake_clock, tmp_path):
    store = storage_mod.Storage(str(tmp_path))
    store.write({"channels": {"old": {"watch_count": 7, "watch_seconds": 900.0,
                                      "tune_count": 7, "last_watched": 500.0,
                                      "last_tuned": 500.0, "first_seen": 100.0}},
                 "meta": {"stats_since": 100.0}}, 900.0)
    col = make_collector(col_mod, sess_mod, storage_mod, fake_redis, fake_clock,
                         tmp_path)
    col.run_tick()
    assert col.sessionizer.channels["old"]["watch_count"] == 7


def test_channels_seen_resets_to_zero_on_a_sample_error(col_mod, sess_mod, storage_mod,
                                                         fake_clock, tmp_path):
    """FIX6: a stale previous channels_seen count must not be left sitting in
    stats behind a failed sample -- it must not read as an unchanged healthy
    channel count."""
    class BreaksOnDemand(FakeRedis):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.fail = False

        def scan_iter(self, match=None, count=None):
            if self.fail:
                raise RuntimeError("redis is down")
            return super().scan_iter(match=match, count=count)

    redis = BreaksOnDemand(clock=fake_clock)
    col = make_collector(col_mod, sess_mod, storage_mod, redis, fake_clock, tmp_path)
    for i in range(5):
        redis.open_channel(f"u{i}", clients=1)
    col.run_tick()
    assert col.stats["channels_seen"] == 5

    redis.fail = True
    fake_clock.advance(15)
    col.run_tick()
    assert col.stats["channels_seen"] == 0


def test_malformed_scan_keys_are_counted_not_silently_dropped(
        col_mod, sess_mod, storage_mod, fake_redis, fake_clock, tmp_path):
    """FIX6: uuid_from_key returns None for a key with an empty uuid segment
    (still matches SCAN_PATTERN's glob, so scan_iter yields it) -- that must
    be counted, not silently dropped with no trace."""
    col = make_collector(col_mod, sess_mod, storage_mod, fake_redis, fake_clock,
                         tmp_path)
    fake_redis.open_channel("u1", clients=1)
    fake_redis.kv["live:channel::metadata"] = "malformed"
    fake_redis.exp["live:channel::metadata"] = fake_clock.wall() + 100.0

    col.run_tick()

    assert col.stats["channels_seen"] == 1          # only the valid channel counted
    assert col.stats["malformed_keys"] == 1


# ---- Fix pass 2 -------------------------------------------------------------


def test_deposed_and_connection_wedged_leader_does_not_write_on_shutdown(
        col_mod, sess_mod, storage_mod, fake_redis, fake_clock, tmp_path):
    """C1 + B4 combined repro.

    C1 (Critical): renew() must FAIL CLOSED on a Redis error -- it is the
    write fence, re-verified immediately before every flush. A worker whose
    OWN connection wedges (a broken socket in THIS worker; Redis itself and
    the lease are healthy for everyone else, and a real competitor
    legitimately takes over while this worker is wedged) must not verify its
    write against a stale cached `owned` belief -- a cached value is not a
    verification. The reviewer's repro: A believes `leader: True, owned:
    True` the whole time; B legitimately takes the key over; A's `renew()`
    can no longer observe that (its own GETs raise) and, pre-fix, fell back
    to the cached `True` and wrote anyway -- two concurrent writers.

    B4 (Important): `_flush()`'s fence had no test of its own -- deleting the
    `renew()` call from `_flush()` (leaving every other defense standing)
    left the whole suite green, because nothing drove `_flush()` while a
    FOREIGN token actually held the lease. `shutdown()` is the one call path
    guaranteed to reach `_flush()` even once fully deposed, so it is also the
    natural driver for that assertion.
    """
    class WedgeableProxy:
        """Wraps the shared fake Redis store so ONE worker's connection can
        wedge (every call raises) while the underlying store -- and every
        OTHER worker's own client -- stays perfectly healthy. Models a
        broken socket in a single uWSGI worker, not a real Redis outage."""

        def __init__(self, real):
            self._real = real
            self.wedged = False

        def __getattr__(self, name):
            if self.wedged:
                def raiser(*a, **k):
                    raise RuntimeError("connection wedged")
                return raiser
            return getattr(self._real, name)

    proxy = WedgeableProxy(fake_redis)
    col = make_collector(col_mod, sess_mod, storage_mod, proxy, fake_clock,
                         tmp_path, token="A")
    fake_redis.open_channel("u1", clients=1)
    col.run_tick()                                  # A legitimately leads + flushes
    assert (tmp_path / "usage.json").exists()
    assert col.lease.owned is True
    before = (tmp_path / "usage.json").read_bytes()

    # A's own connection wedges. 70s pass -- past LEASE_TTL=60 -- and B
    # legitimately takes over in the REAL store; A's cached belief never
    # observes this because its own GETs now raise instead of returning "B".
    proxy.wedged = True
    fake_clock.advance(70)
    other = col_mod.Lease(fake_redis, "B")
    assert other.tick(fake_clock.wall()) is True
    assert fake_redis.get(col_mod.LEADER_KEY) == "B"
    assert col._was_leader is True                  # A never observed the loss
    assert col.lease.owned is True                  # still a stale cached belief

    col.shutdown()

    after = (tmp_path / "usage.json").read_bytes()
    assert after == before                          # must NOT have been overwritten
    assert col.stats["redis_errors"] >= 1
    assert col.stats["last_error"]
    assert fake_redis.get(col_mod.LEADER_KEY) == "B"   # B's lease untouched


def test_sampling_cadence_gate_sheds_load_while_throttled(
        col_mod, sess_mod, storage_mod, fake_redis, fake_clock, tmp_path, monkeypatch):
    """B1: the `_last_sample` cadence gate (FIX3) is what actually sheds load
    once throttled -- deleting it left the whole suite green, i.e. nothing
    proved "throttling still sheds load" once `base_tick()` decoupled the
    lease-renewal cadence from the sampling cadence. Drive `run_tick()` on
    `base_tick()`'s own cadence (as the real thread loop would) while pinned
    to the throttle ceiling and count actual SCANs (`scan_iter` calls), not
    just an internal counter."""
    col = make_collector(col_mod, sess_mod, storage_mod, fake_redis, fake_clock,
                         tmp_path)
    col.effective_interval = col_mod.THROTTLE_CEILING_S    # 120s, pinned
    assert col.base_tick() == col_mod.LEASE_TTL / 3.0       # 20s renewal cadence
    # Keep _throttle() a no-op for the whole run (grey zone strictly between
    # SOFT_BUDGET_MS and HARD_BUDGET_MS) -- otherwise real wall-clock timing
    # of this in-memory test would eventually trip the un-throttle path and
    # silently change the cadence mid-run.
    monkeypatch.setattr(col, "_cycle_ms",
                        lambda start: (col_mod.SOFT_BUDGET_MS + col_mod.HARD_BUDGET_MS) / 2.0)

    scan_calls = []
    real_scan_iter = fake_redis.scan_iter

    def counting_scan_iter(*a, **k):
        scan_calls.append(1)
        return real_scan_iter(*a, **k)
    fake_redis.scan_iter = counting_scan_iter

    step = col.base_tick()
    n_calls = 60
    for _ in range(n_calls):
        col.run_tick()
        fake_clock.advance(step)

    assert len(scan_calls) < n_calls // 2            # far fewer SCANs than run_tick calls
    assert len(scan_calls) == col.stats["cycle_seq"]
    assert col.effective_interval == col_mod.THROTTLE_CEILING_S   # never reverted


def test_observe_is_given_effective_interval_not_configured(
        col_mod, sess_mod, storage_mod, fake_redis, fake_clock, tmp_path):
    """B2: swapping `self.effective_interval` -> `self.configured_interval`
    in the `observe()` call left the suite green. This is load-bearing: the
    sessionizer's grace floor is `2 * effective_interval`, so passing the
    wrong interval silently shreds sessions under throttling."""
    col = make_collector(col_mod, sess_mod, storage_mod, fake_redis, fake_clock,
                         tmp_path)
    col.effective_interval = 999.0
    assert col.effective_interval != col.configured_interval

    seen = []
    real_observe = col.sessionizer.observe

    def spy_observe(now, present, effective_interval=None):
        seen.append(effective_interval)
        return real_observe(now, present, effective_interval)
    col.sessionizer.observe = spy_observe

    fake_redis.open_channel("u1", clients=1)
    col.run_tick()

    assert seen == [999.0]


def test_lease_error_increments_redis_errors_via_drain(
        col_mod, sess_mod, storage_mod, fake_clock, tmp_path):
    """B3: mutation-tested -- stubbing out `stats["redis_errors"] += 1`
    inside `_drain_lease_error()` left the suite green. Isolate the
    lease-error path from the already-covered sample-error path
    (`test_redis_error_is_counted_not_raised`): the lease's `.get()` fails on
    the very first call, so `tick()` returns `False` (never acquired --
    `owned` stays at its initial `False`) and `run_tick()` returns before
    ever reaching `_sample()` -- so `redis_errors` can only have come from
    `_drain_lease_error()`."""
    class FlakyOnce(FakeRedis):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.fail_next_get = True

        def get(self, key):
            if self.fail_next_get:
                self.fail_next_get = False
                raise RuntimeError("redis blip")
            return super().get(key)

    redis = FlakyOnce(clock=fake_clock)
    col = make_collector(col_mod, sess_mod, storage_mod, redis, fake_clock, tmp_path)
    col.run_tick()

    assert col._was_leader is False
    assert col.stats["redis_errors"] == 1
    assert col.stats["last_error"]


def test_corrupt_but_parseable_record_does_not_kill_collection(
        col_mod, sess_mod, storage_mod, fake_redis, fake_clock, tmp_path):
    """usage.json can be valid JSON while one record is malformed (a hand
    edit, a partial write). _acquire_state used to hand it raw to the
    sessionizer, whose tune_count += 1 then raised on every tick, outside
    run_tick's sample guard -- collection dead forever while reports (which
    sanitize their own copy) kept rendering fine."""
    store = storage_mod.Storage(str(tmp_path))
    store.write({"channels": {
        "u1": {"watch_count": "3", "watch_seconds": None, "tune_count": None,
               "last_watched": "not a timestamp", "last_tuned": None,
               "first_seen": None},
        "u2": None},
        "meta": {"stats_since": 500.0, "coverage": {"2026-01-01T00": None}}},
        fake_clock.wall())
    sess = sess_mod.Sessionizer(dict(THRESHOLDS))
    col = col_mod.Collector(fake_redis, sess, store, dict(THRESHOLDS),
                            token="me", wall=fake_clock.wall)

    fake_redis.open_channel("u1", clients=1)
    col.run_tick()                      # acquire leadership + load state
    fake_clock.advance(16.0)
    col.run_tick()                      # observe() credits u1: must not raise

    assert col.sessionizer.channels["u1"]["tune_count"] == 1
    assert col.sessionizer.channels["u1"]["watch_count"] == 3
    assert col.sessionizer.channels["u2"]["tune_count"] == 0


def test_acquire_state_preserves_unknown_per_channel_keys(
        col_mod, sess_mod, storage_mod, fake_redis, fake_clock, tmp_path):
    """channels is loaded and re-emitted whole; a future release stores new
    per-channel data inside it, so sanitizing must coerce the known keys
    without discarding unknown ones."""
    store = storage_mod.Storage(str(tmp_path))
    store.write({"channels": {
        "u1": {"watch_count": 1, "watch_seconds": 60.0, "tune_count": 1,
               "last_watched": 400.0, "last_tuned": 400.0, "first_seen": 300.0,
               "future_key": {"kept": True}}},
        "meta": {"stats_since": 500.0, "coverage": {}}},
        fake_clock.wall())
    sess = sess_mod.Sessionizer(dict(THRESHOLDS))
    col = col_mod.Collector(fake_redis, sess, store, dict(THRESHOLDS),
                            token="me", wall=fake_clock.wall)
    col.run_tick()

    assert col.sessionizer.channels["u1"]["future_key"] == {"kept": True}
