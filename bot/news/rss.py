"""RSS-новини: опитування, кеш, дедуплікація, фільтр за ключовими словами."""

from __future__ import annotations

import asyncio
import calendar as _cal
import html
import logging
import re
import time
from dataclasses import dataclass

import feedparser
import httpx

from bot.config import NetworkConfig, NewsConfig, NewsFeed

log = logging.getLogger(__name__)

KEEP_HOURS = 6
FUTURE_TOLERANCE_SEC = 300
USER_AGENT = "Mozilla/5.0 (compatible; usousdt-bot/1.0)"


@dataclass
class NewsItem:
    title: str
    link: str
    source: str
    published: float      # unix-секунди, UTC
    first_seen: float
    text: str             # заголовок + опис, для пошуку ключових слів
    dated: bool = True    # False — у стрічці немає дати, published = час першої появи


def normalize_title(title: str) -> str:
    # Google News додає " - Джерело" в кінці; прибираємо, щоб ловити дублікати між стрічками.
    t = re.sub(r"\s+-\s+[^-]{2,40}$", "", title.strip())
    return re.sub(r"[^\w]+", " ", t.lower()).strip()


def keyword_regex(keywords: list[str]) -> re.Pattern[str]:
    parts = sorted({re.escape(k.lower()) for k in keywords}, key=len, reverse=True)
    return re.compile(r"\b(" + "|".join(parts) + r")\b", re.IGNORECASE)


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def parse_feed(content: bytes, feed: NewsFeed, now: float) -> list[NewsItem]:
    parsed = feedparser.parse(content)
    items = []
    for e in parsed.entries:
        title = _strip_html(e.get("title", "")).strip()
        if not title:
            continue
        ts_struct = e.get("published_parsed") or e.get("updated_parsed")
        published = float(_cal.timegm(ts_struct)) if ts_struct else now
        if published > now + FUTURE_TOLERANCE_SEC:
            published = now  # кривий часовий пояс у стрічці
        text = f"{title} {_strip_html(e.get('summary', ''))}"
        items.append(NewsItem(title, e.get("link", ""), feed.name, published, now, text, bool(ts_struct)))
    return items


class NewsService:
    def __init__(self, cfg: NewsConfig, network: NetworkConfig, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.network = network
        self._client = client or httpx.AsyncClient(
            timeout=network.timeout_sec, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        )
        self._regex = keyword_regex(cfg.keywords)
        self._items: dict[str, NewsItem] = {}
        # Пам'ять дедуплікації: ключ -> коли востаннє бачили в будь-якій стрічці.
        self._seen: dict[str, float] = {}
        self._polled_feeds: set[str] = set()
        self.feed_ok: dict[str, bool] = {}
        self.last_poll: float | None = None
        self._stop = asyncio.Event()

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def available(self) -> bool:
        return self.last_poll is not None and any(self.feed_ok.values())

    async def _fetch(self, feed: NewsFeed) -> bytes:
        last: Exception | None = None
        for attempt in range(self.network.retries + 1):
            try:
                r = await self._client.get(feed.url)
                r.raise_for_status()
                return r.content
            except httpx.HTTPError as exc:
                last = exc
                if attempt < self.network.retries:
                    await asyncio.sleep(self.network.backoff_base_sec * 2**attempt)
        raise last  # type: ignore[misc]

    @staticmethod
    def _keys(it: NewsItem) -> list[str]:
        keys = [f"t:{normalize_title(it.title)}"]
        if it.link:
            keys.append(f"l:{it.link}")
        return keys

    def ingest(self, items: list[NewsItem], first_poll: bool = False) -> int:
        added = 0
        for it in items:
            keys = self._keys(it)
            known = any(k in self._seen for k in keys)
            for k in keys:
                self._seen[k] = it.first_seen
            if known:
                continue
            if not it.dated and first_poll:
                # Архів недатованої стрічки при старті — не «свіжі» новини.
                it.published = it.first_seen - KEEP_HOURS * 3600
            self._items[keys[0]] = it
            added += 1
        return added

    def prune(self, now: float) -> None:
        cutoff = now - KEEP_HOURS * 3600
        self._items = {k: it for k, it in self._items.items() if it.published >= cutoff}
        self._seen = {k: ts for k, ts in self._seen.items() if ts >= now - 24 * 3600}

    async def poll_once(self) -> int:
        now = time.time()
        results = await asyncio.gather(*(self._fetch(f) for f in self.cfg.feeds), return_exceptions=True)
        added = 0
        for feed, res in zip(self.cfg.feeds, results):
            if isinstance(res, BaseException):
                if self.feed_ok.get(feed.name, True):
                    log.warning("RSS %s недоступна: %r", feed.name, res)
                self.feed_ok[feed.name] = False
                continue
            self.feed_ok[feed.name] = True
            added += self.ingest(parse_feed(res, feed, now), first_poll=feed.name not in self._polled_feeds)
            self._polled_feeds.add(feed.name)
        self.prune(now)
        self.last_poll = now
        log.debug("RSS: +%d новин, у кеші %d", added, len(self._items))
        return added

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001
                log.exception("RSS: помилка опитування")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.poll_interval_sec)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    def headlines(self, now: float | None = None) -> list[dict]:
        """Свіжі релевантні заголовки, найновіші спочатку."""
        now = now or time.time()
        cutoff = now - self.cfg.max_age_min * 60
        fresh = [it for it in self._items.values() if it.published >= cutoff and self._regex.search(it.text)]
        fresh.sort(key=lambda it: it.published, reverse=True)
        return [
            {
                "age_min": int((now - it.published) // 60),
                "time_utc": time.strftime("%H:%M", time.gmtime(it.published)),
                "source": it.source,
                "title": it.title,
            }
            for it in fresh[: self.cfg.max_headlines]
        ]
