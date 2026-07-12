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
    assert col.effective_interval > THRESHOLDS["poll_interval_s"]
    assert col.effective_interval <= col_mod.THROTTLE_CEILING_S


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


def test_shutdown_releases_only_its_own_lease(col_mod, sess_mod, storage_mod,
                                              fake_redis, fake_clock, tmp_path):
    col = make_collector(col_mod, sess_mod, storage_mod, fake_redis, fake_clock,
                         tmp_path, token="me")
    col.run_tick()
    col.shutdown()
    assert fake_redis.get(col_mod.LEADER_KEY) is None


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
