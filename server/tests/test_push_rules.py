"""push/rules.py: each sending rule, with a frozen clock."""
import copy
from datetime import datetime, timedelta, timezone

from push import config, rules
from push.rules import History, Story

R = config.DEFAULTS["rules"]
IST = "Asia/Kolkata"


def utc(h, m=0, day=16):
    return datetime(2026, 11, day, h, m, tzinfo=timezone.utc)


def story(eid="ev_a", score=0.5, spice=8, importance=7, age_min=30, now=None):
    return Story(eid, score, spice, importance, (now or NOON) - timedelta(minutes=age_min))


NOON = utc(6, 30)       # 12:00 in India


def test_a_fresh_spicy_story_is_sent():
    d = rules.decide(NOON, IST, [story()], History(), R)
    assert (d.action, d.event_id) == ("send", "ev_a")


def test_spice_below_the_floor_is_never_sent():
    assert rules.decide(NOON, IST, [story(spice=6)], History(), R).action == "none"
    assert rules.decide(NOON, IST, [story(spice=7)], History(), R).action == "send"


def test_a_story_older_than_three_hours_by_news_date_is_not_sent():
    assert rules.decide(NOON, IST, [story(age_min=181)], History(), R).reason == "nothing_fresh"
    assert rules.decide(NOON, IST, [story(age_min=179)], History(), R).action == "send"


def test_the_same_story_is_never_sent_twice():
    h = History(got={"ev_a"})
    assert rules.decide(NOON, IST, [story()], h, R).action == "none"


def test_the_best_score_goes_first():
    d = rules.decide(NOON, IST, [story("ev_a", 0.2), story("ev_b", 0.9), story("ev_c", 0.4)], History(), R)
    assert d.event_id == "ev_b"


def test_gap_between_alerts():
    h = History(got={"ev_old"}, sent_local_dates=["2026-11-16"],
                last_sent_at=NOON - timedelta(minutes=R["min_gap_min"] - 1))
    assert rules.decide(NOON, IST, [story()], h, R).reason == "gap"
    h.last_sent_at = NOON - timedelta(minutes=R["min_gap_min"])
    assert rules.decide(NOON, IST, [story()], h, R).action == "send"


def test_daily_cap_counts_the_devices_local_day():
    long_ago = NOON - timedelta(hours=5)
    full = History(sent_local_dates=["2026-11-16"] * R["daily_cap"], last_sent_at=long_ago)
    assert rules.decide(NOON, IST, [story()], full, R).reason == "daily_cap"
    # The same two alerts on the local day before do not count: 20:00 UTC on the 15th is the 16th in India.
    yesterday = History(sent_local_dates=["2026-11-15"] * R["daily_cap"], last_sent_at=long_ago)
    assert rules.decide(NOON, IST, [story()], yesterday, R).action == "send"


def test_importance_nine_may_exceed_the_cap_but_not_the_hard_cap_or_the_gap():
    long_ago = NOON - timedelta(hours=5)
    full = History(sent_local_dates=["2026-11-16"] * R["daily_cap"], last_sent_at=long_ago)
    big = story("ev_big", importance=9)
    assert rules.decide(NOON, IST, [story(), big], full, R).event_id == "ev_big"
    hard = History(sent_local_dates=["2026-11-16"] * R["daily_cap_hard"], last_sent_at=long_ago)
    assert rules.decide(NOON, IST, [big], hard, R).reason == "daily_cap"
    recent = History(sent_local_dates=["2026-11-16"] * R["daily_cap"], last_sent_at=NOON - timedelta(minutes=10))
    assert rules.decide(NOON, IST, [big], recent, R).reason == "gap"


def test_quiet_hours_hold_instead_of_sending_even_importance_ten():
    night = utc(17, 0)                         # 22:30 in India
    d = rules.decide(night, IST, [story(importance=10, now=night)], History(), R)
    assert (d.action, d.event_id, d.reason) == ("hold", "ev_a", "quiet_hours")
    # ... and the same instant is daytime in Los Angeles (09:00), where it is sent.
    assert rules.decide(night, "America/Los_Angeles", [story(now=night)], History(), R).action == "send"


def test_quiet_hours_edges():
    for h, m, quiet in ((16, 29, False), (16, 30, True), (1, 59, True), (2, 0, False)):   # 21:59 22:00 07:29 07:30 IST
        now = utc(h, m)
        assert rules.in_quiet(rules.local(now, IST), R) is quiet, (h, m)


def test_only_the_single_best_story_is_held_and_it_is_sent_in_the_morning():
    night = utc(18, 0)                         # 23:30 IST
    a, b = story("ev_a", 0.4, now=night), story("ev_b", 0.8, now=night)
    assert rules.decide(night, IST, [a, b], History(), R).event_id == "ev_b"
    # A weaker story arriving later does not replace it; a better one does.
    later = night + timedelta(hours=1)
    weak, strong = story("ev_c", 0.5, now=later), story("ev_d", 0.95, now=later)
    assert rules.decide(later, IST, [a, b, weak], History(held="ev_b"), R).action == "none"
    assert rules.decide(later, IST, [a, b, strong], History(held="ev_b"), R).event_id == "ev_d"
    # 07:30 IST next morning: the held story is 8 h old, over max_age_h, and is still the one sent.
    morning = utc(2, 0, day=17)
    d = rules.decide(morning, IST, [a, b], History(held="ev_b"), R)
    assert (d.action, d.event_id, d.reason) == ("send", "ev_b", "held_released")
    # The story that was not held is simply too old by then.
    assert rules.decide(morning, IST, [a], History(), R).action == "none"


def test_a_held_story_too_old_even_for_the_morning_is_dropped():
    morning = utc(2, 0, day=17)
    old = Story("ev_a", 0.9, 8, 7, morning - timedelta(hours=R["held_max_age_h"], minutes=1))
    assert rules.decide(morning, IST, [old], History(held="ev_a"), R).action == "none"


def test_every_limit_is_a_config_value():
    r = copy.deepcopy(R)
    r.update(min_spice=9, daily_cap=6, min_gap_min=30, quiet_start="23:00", quiet_end="07:00", max_age_h=1)
    assert rules.decide(NOON, IST, [story(spice=8)], History(), r).action == "none"
    assert rules.decide(NOON, IST, [story(spice=9, age_min=61)], History(), r).action == "none"
    h = History(sent_local_dates=["2026-11-16"] * 5, last_sent_at=NOON - timedelta(minutes=31))
    assert rules.decide(NOON, IST, [story(spice=9)], h, r).action == "send"
    assert rules.in_quiet(rules.local(utc(17, 0), IST), r) is False       # 22:30 IST is no longer quiet


def test_time_zones_iana_fixed_offset_and_unknown():
    now = utc(12, 0)
    assert rules.local(now, "Asia/Kolkata").hour == 17
    assert rules.local(now, "GMT+0530").strftime("%H:%M") == "17:30"
    assert rules.local(now, "GMT-8").hour == 4
    assert rules.local(now, "Not/AZone").hour == 12
    # Daylight saving: Los Angeles is UTC-8 in November and UTC-7 in July.
    assert rules.local(datetime(2026, 7, 1, 12, tzinfo=timezone.utc), "America/Los_Angeles").hour == 5


def test_a_story_that_was_out_when_the_device_registered_is_never_sent_or_held():
    reg = NOON - timedelta(minutes=10)
    before = Story("ev_before", 0.9, 9, 9, NOON - timedelta(minutes=30), NOON - timedelta(minutes=20))
    after = Story("ev_after", 0.1, 8, 7, NOON - timedelta(minutes=30), NOON - timedelta(minutes=5))
    d = rules.decide(NOON, IST, [before, after], History(registered_at=reg), R)
    assert (d.action, d.event_id) == ("send", "ev_after")       # by when it came out here, not its news date
    assert rules.decide(NOON, IST, [before], History(registered_at=reg), R).action == "none"
    assert rules.decide(NOON, IST, [before], History(registered_at=reg - timedelta(hours=1)), R).action == "send"
    night = utc(18)                                             # 23:30 IST
    old = Story("ev_old", 0.9, 9, 9, night - timedelta(minutes=30), night - timedelta(minutes=20))
    assert rules.decide(night, IST, [old], History(registered_at=night - timedelta(minutes=1)), R).action == "none"
    # Without a queue time the news date stands in.
    assert rules.decide(NOON, IST, [story()], History(registered_at=NOON), R).action == "none"
    assert rules.decide(NOON, IST, [story()], History(registered_at=NOON - timedelta(hours=1)), R).action == "send"


def test_old_zone_names_are_not_utc():
    """iOS still reports names like Asia/Calcutta; quiet hours must follow the phone, not UTC."""
    from datetime import datetime, timezone
    from push import rules
    at = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
    for name in ("Asia/Calcutta", "Asia/Katmandu", "Europe/Kiev", "US/Pacific"):
        assert at.astimezone(rules.zone(name)).utcoffset().total_seconds() != 0, name


def test_no_daily_cap_leaves_the_gap_and_the_night_as_the_only_limits():
    r = dict(R, daily_cap=0, daily_cap_hard=0)
    many = History(sent_local_dates=["2026-11-16"] * 25, last_sent_at=NOON - timedelta(minutes=r["min_gap_min"]))
    assert rules.decide(NOON, IST, [story()], many, r).action == "send"
    close = History(sent_local_dates=["2026-11-16"] * 25, last_sent_at=NOON - timedelta(minutes=1))
    assert rules.decide(NOON, IST, [story()], close, r).reason == "gap"
