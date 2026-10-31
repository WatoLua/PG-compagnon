from datetime import datetime, timedelta

from site_sync import appeared_at


def test_appeared_at_today_in_local_timezone():
    t = (datetime.now() - timedelta(minutes=10)).replace(second=0, microsecond=0)
    iso = appeared_at(t.strftime("%H:%M"))
    assert datetime.fromisoformat(iso) == t.astimezone()


def test_time_in_the_future_is_yesterday():
    t = (datetime.now() + timedelta(hours=1)).replace(second=0, microsecond=0)
    iso = appeared_at(t.strftime("%H:%M"))
    assert datetime.fromisoformat(iso).date() == (t - timedelta(days=1)).date()


def test_unreadable_time():
    assert appeared_at("") is None
    assert appeared_at("25:99") is None
