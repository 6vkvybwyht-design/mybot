import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from bot.config import CalendarConfig, NewsFeed
from bot.news.calendar import Event, EventCalendar, parse_event_command
from bot.news.rss import NewsService, normalize_title, parse_feed
from tests.helpers import CFG

UTC = timezone.utc


def rss(items):
    body = "".join(
        f"<item><title>{t}</title><link>{l}</link>"
        + (f"<pubDate>{time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime(ts))}</pubDate>" if ts else "")
        + f"<description>{d}</description></item>"
        for t, l, ts, d in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'.encode()


def test_parse_feed_dates_and_html():
    now = time.time()
    items = parse_feed(rss([("Oil &amp; gas <b>up</b>", "http://a", now - 600, "desc"),
                            ("No date", "http://b", None, "")]), NewsFeed(name="F", url="u"), now)
    assert items[0].title == "Oil & gas up"
    assert abs(items[0].published - (now - 600)) < 2
    assert items[1].published == now  # без дати — час першої появи


def test_normalize_title_strips_source_suffix():
    assert normalize_title("Oil jumps 3% - Reuters") == normalize_title("Oil jumps 3%")


async def test_news_service_filter_dedupe_age():
    now = time.time()
    feed_a = rss([
        ("Crude oil jumps after OPEC cut", "http://1", now - 300, ""),
        ("Football results", "http://2", now - 300, ""),
        ("Turmoil in fashion industry", "http://3", now - 300, ""),   # 'oil' всередині слова — не рахується
        ("Old oil news", "http://4", now - 3 * 3600, ""),
        ("Markets calm", "http://5", now - 60, "Iran sanctions weigh on supply"),  # ключове слово в описі
    ])
    feed_b = rss([("Crude oil jumps after OPEC cut - Reuters", "http://other", now - 200, "")])

    def handler(req):
        if req.url.host == "a":
            return httpx.Response(200, content=feed_a)
        if req.url.host == "b":
            return httpx.Response(200, content=feed_b)
        return httpx.Response(503)

    cfg = CFG.news.model_copy(update={"feeds": [NewsFeed(name="A", url="http://a/rss"),
                                                 NewsFeed(name="B", url="http://b/rss"),
                                                 NewsFeed(name="Down", url="http://down/rss")]})
    net = CFG.network.model_copy(update={"retries": 0})
    svc = NewsService(cfg, net, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert not svc.available
    await svc.poll_once()
    assert svc.available and svc.feed_ok == {"A": True, "B": True, "Down": False}
    titles = [h["title"] for h in svc.headlines(now)]
    assert titles == ["Markets calm", "Crude oil jumps after OPEC cut"]
    # повторне опитування не дублює
    assert await svc.poll_once() == 0


async def test_news_all_feeds_down():
    cfg = CFG.news.model_copy(update={"feeds": [NewsFeed(name="Down", url="http://down/rss")]})
    net = CFG.network.model_copy(update={"retries": 0})
    svc = NewsService(cfg, net, httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    await svc.poll_once()
    assert not svc.available


def test_undated_items_old_on_first_poll_fresh_later():
    now = time.time()
    feed = NewsFeed(name="F", url="u")
    svc = NewsService(CFG.news, CFG.network)
    svc.last_poll, svc.feed_ok = now, {"F": True}
    svc.ingest(parse_feed(rss([("Oil archive story", "http://old", None, "")]), feed, now), first_poll=True)
    assert svc.headlines(now) == []
    svc.ingest(parse_feed(rss([("Oil archive story", "http://old", None, ""),
                               ("Oil breaking story", "http://new", None, "")]), feed, now + 60))
    assert [h["title"] for h in svc.headlines(now + 60)] == ["Oil breaking story"]
    # через 7 годин архівна новина, яка досі в стрічці, не «воскресає»
    later = now + 7 * 3600
    svc.prune(later)
    svc.ingest(parse_feed(rss([("Oil archive story", "http://old", None, "")]), feed, later))
    assert svc.headlines(later) == []


def test_news_limit():
    now = time.time()
    svc = NewsService(CFG.news, CFG.network)
    svc.ingest(parse_feed(rss([(f"Oil story {i}", f"http://{i}", now - i, "") for i in range(30)]),
                          NewsFeed(name="F", url="u"), now))
    svc.last_poll, svc.feed_ok = now, {"F": True}
    assert len(svc.headlines(now)) == CFG.news.max_headlines


# ---------- календар ----------

def test_eia_follows_new_york_dst():
    cal = EventCalendar(CFG.calendar)
    summer = cal.upcoming(datetime(2026, 7, 6, tzinfo=UTC), hours=24 * 7)
    winter = cal.upcoming(datetime(2026, 12, 7, tzinfo=UTC), hours=24 * 7)
    eia_s = [e for e in summer if "EIA" in e.name]
    eia_w = [e for e in winter if "EIA" in e.name]
    assert eia_s[0].at == datetime(2026, 7, 8, 14, 30, tzinfo=UTC)
    assert eia_w[0].at == datetime(2026, 12, 9, 15, 30, tzinfo=UTC)


def test_calendar_near_and_snapshot():
    cfg = CalendarConfig(recurring=[], one_time=[{"name": "ФРС", "at": "2026-10-28 18:00"}])
    cal = EventCalendar(cfg, [Event("Моя подія", datetime(2026, 10, 28, 12, 0, tzinfo=UTC), "user")])
    t = datetime(2026, 10, 28, 17, 50, tzinfo=UTC)
    assert [e.name for e in cal.near(t, 15, 15)] == ["ФРС"]
    assert cal.near(t - timedelta(minutes=10), 15, 15) == []
    assert [e.name for e in cal.near(t + timedelta(minutes=24), 15, 15)] == ["ФРС"]
    snap = cal.snapshot(t, 15)
    assert snap["upcoming_24h"] == [{"name": "ФРС", "in_min": 10}]
    assert snap["event_risk_window"] is True
    assert cal.next_event(datetime(2026, 10, 28, 11, 0, tzinfo=UTC)).name == "Моя подія"


def test_parse_event_command():
    e = parse_event_command("/event 2026-10-28 18:00 Рішення ФРС щодо ставки")
    assert e.at == datetime(2026, 10, 28, 18, 0, tzinfo=UTC) and e.name == "Рішення ФРС щодо ставки"
    with pytest.raises(ValueError):
        parse_event_command("/event 2026-10-28 Рішення")
    with pytest.raises(ValueError):
        parse_event_command("/event 28.10.2026 18:00 Рішення")
