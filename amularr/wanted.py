"""Search aMule for what Sonarr and Radarr are waiting for.

ed2k/Kad has no "recent releases" feed, so the Torznab RSS answer alone
can never surface a freshly aired episode: something has to search for
it. This module asks the *arr apps for their wanted items (monitored,
missing, aired or released recently), runs the matching keyword searches
in a background thread and keeps the hits, so the next RSS sync sees
them and the *arr app grabs them on its own.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator

from .config import Config
from .ec import ECError, SearchResult
from .search import SearchService

log = logging.getLogger(__name__)

SOURCE_TV = "tv"
SOURCE_MOVIES = "movies"
SOURCES = (SOURCE_TV, SOURCE_MOVIES)

# (base url, api key, query params) -> decoded JSON
Fetcher = Callable[[str, str, dict[str, str]], object]


@dataclass
class WantedItem:
    key: str  # stable id across refreshes, e.g. "tv:1234"
    source: str
    label: str  # human readable, for logs
    queries: list[str]
    last_search: float | None = None  # time.monotonic() of the last aMule search
    results: list[SearchResult] = field(default_factory=list)


def http_json(url: str, api_key: str, params: dict[str, str]) -> object:
    full = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    request = urllib.request.Request(full, headers={"X-Api-Key": api_key, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def parse_date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            out.append(value)
    return out


# ----------------------------------------------------------------------
# Wanted lists -> keyword searches
# ----------------------------------------------------------------------


def series_titles(series: dict, season: int, max_titles: int) -> list[tuple[str, int]]:
    """Titles Sonarr would search with for ``season``: the series title
    plus the scene/alternate titles that apply to that season, each with
    the season number the release group uses for it."""
    from .torznab import clean_query

    titles: list[tuple[str, int]] = []
    seen: set[str] = set()

    def add(title: object, scene_season: int) -> None:
        cleaned = clean_query(str(title or ""))
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            titles.append((cleaned, scene_season))

    add(series.get("title"), season)
    for alt in series.get("alternateTitles") or []:
        alt_season = alt.get("seasonNumber")
        if alt_season not in (None, -1, season):
            continue
        scene_season = alt.get("sceneSeasonNumber")
        add(alt.get("title"), scene_season if isinstance(scene_season, int) and scene_season >= 0 else season)
    return titles[:max_titles]


def sonarr_items(config: Config, fetch: Fetcher, now: datetime) -> Iterator[WantedItem]:
    from .torznab import build_queries

    url = config.sonarr_url.rstrip("/") + "/api/v3/wanted/missing"  # type: ignore[union-attr]
    params = {
        "page": "1",
        "pageSize": str(config.wanted_page_size),
        "sortKey": "airDateUtc",
        "sortDirection": "descending",
        "includeSeries": "true",
        "monitored": "true",
    }
    data = fetch(url, config.sonarr_api_key or "", params)
    since = now - timedelta(days=config.wanted_days)
    for record in (data.get("records") if isinstance(data, dict) else None) or []:
        aired = parse_date(record.get("airDateUtc"))
        if aired is None or aired < since or aired > now:
            continue
        season = record.get("seasonNumber")
        episode = record.get("episodeNumber")
        if not isinstance(season, int) or not isinstance(episode, int) or season <= 0:
            continue  # specials have no usable scene numbering
        series = record.get("series") or {}
        queries: list[str] = []
        for title, scene_season in series_titles(series, season, config.wanted_max_titles):
            queries += build_queries("tvsearch", title, str(scene_season), str(episode), None)
        if not queries:
            continue
        yield WantedItem(
            key=f"{SOURCE_TV}:{record.get('id')}",
            source=SOURCE_TV,
            label=f"{series.get('title')} S{season:02d}E{episode:02d}",
            queries=_dedupe(queries),
        )


def movie_titles(movie: dict, languages: set[str], max_titles: int) -> list[str]:
    from .torznab import clean_query

    raw: list[str] = [str(movie.get("title") or ""), str(movie.get("originalTitle") or "")]
    for alt in movie.get("alternateTitles") or []:
        language = (alt.get("language") or {}).get("name", "")
        if str(language).lower() in languages:
            raw.append(str(alt.get("title") or ""))
    return _dedupe([clean_query(t) for t in raw])[:max_titles]


def radarr_items(config: Config, fetch: Fetcher, now: datetime) -> Iterator[WantedItem]:
    from .torznab import build_queries

    url = config.radarr_url.rstrip("/") + "/api/v3/wanted/missing"  # type: ignore[union-attr]
    params = {"page": "1", "pageSize": str(config.wanted_page_size), "monitored": "true"}
    data = fetch(url, config.radarr_api_key or "", params)
    since = now - timedelta(days=config.wanted_days)
    languages = {lang.lower() for lang in config.wanted_title_languages}
    for movie in (data.get("records") if isinstance(data, dict) else None) or []:
        dates = [parse_date(movie.get(k)) for k in ("digitalRelease", "physicalRelease", "inCinemas", "added")]
        if not any(d is not None and since <= d <= now for d in dates):
            continue
        year = movie.get("year")
        queries: list[str] = []
        for title in movie_titles(movie, languages, config.wanted_max_titles):
            built = build_queries("movie", title, None, None, str(year) if isinstance(year, int) else None)
            if built:
                queries.append(built[0])  # "<title> <year>" only; the bare title is too broad for a feed
        if not queries:
            continue
        yield WantedItem(
            key=f"{SOURCE_MOVIES}:{movie.get('id')}",
            source=SOURCE_MOVIES,
            label=f"{movie.get('title')} ({year})",
            queries=_dedupe(queries),
        )


# ----------------------------------------------------------------------
# Background searcher
# ----------------------------------------------------------------------


class WantedSearcher:
    def __init__(self, search: SearchService, config: Config, fetch: Fetcher = http_json, clock: Callable[[], float] = time.monotonic):
        self.search = search
        self.config = config
        self.fetch = fetch
        self.clock = clock
        self._lock = threading.Lock()
        self._items: dict[str, WantedItem] = {}
        self._last_refresh: dict[str, float] = {}
        self._running = False

    # configuration ----------------------------------------------------

    def enabled(self, source: str) -> bool:
        cfg = self.config
        if source == SOURCE_TV:
            return bool(cfg.sonarr_url and cfg.sonarr_api_key)
        if source == SOURCE_MOVIES:
            return bool(cfg.radarr_url and cfg.radarr_api_key)
        return False

    def sources(self) -> list[str]:
        return [s for s in SOURCES if self.enabled(s)]

    # feed side --------------------------------------------------------

    def results(self, sources: list[str] | None = None) -> list[SearchResult]:
        """Every hit of every wanted item, most recently searched first."""
        wanted = set(sources) if sources else set(SOURCES)
        seen: set[str] = set()
        out: list[SearchResult] = []
        with self._lock:
            items = sorted((i for i in self._items.values() if i.source in wanted), key=lambda i: -(i.last_search or 0))
            for item in items:
                for result in item.results:
                    if result.hash not in seen:
                        seen.add(result.hash)
                        out.append(result)
        return out

    def maybe_refresh(self, sources: list[str] | None = None, wait: bool = False) -> bool:
        """Start a background refresh of the given sources if they are
        configured and their last refresh is older than the interval.
        Returns True when a refresh was started."""
        now = self.clock()
        with self._lock:
            if self._running:
                return False
            due = [
                s
                for s in (sources or SOURCES)
                if self.enabled(s) and (s not in self._last_refresh or now - self._last_refresh[s] >= self.config.wanted_interval)
            ]
            if not due:
                return False
            self._running = True
            for s in due:  # stamp before running so a failing refresh does not spin
                self._last_refresh[s] = now
        thread = threading.Thread(target=self._refresh, args=(due,), name="amularr-wanted", daemon=True)
        thread.start()
        if wait:
            thread.join()
        return True

    # refresh ----------------------------------------------------------

    def _refresh(self, sources: list[str]) -> None:
        try:
            for source in sources:
                self._refresh_source(source)
        except Exception:  # noqa: BLE001 - background thread, never die silently
            log.exception("wanted refresh failed")
        finally:
            with self._lock:
                self._running = False

    def _collect(self, source: str) -> list[WantedItem]:
        now = datetime.now(timezone.utc)
        if source == SOURCE_TV:
            return list(sonarr_items(self.config, self.fetch, now))
        return list(radarr_items(self.config, self.fetch, now))

    def _refresh_source(self, source: str) -> None:
        cfg = self.config
        try:
            fresh = self._collect(source)
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("wanted %s: cannot read the wanted list: %s", source, exc)
            return

        now = self.clock()
        with self._lock:
            old = {k: v for k, v in self._items.items() if v.source == source}
            for key in old:
                del self._items[key]  # dropped unless still wanted below
            for item in fresh:
                previous = old.get(item.key)
                self._items[item.key] = previous if previous is not None and previous.queries == item.queries else item
            due = [
                i
                for i in self._items.values()
                if i.source == source and (i.last_search is None or now - i.last_search >= cfg.wanted_research_interval)
            ]
        due.sort(key=lambda i: (i.last_search is not None, i.last_search or 0))

        budget = cfg.wanted_max_searches
        searched = 0
        hits = 0
        file_type = cfg.file_type
        for item in due:
            if len(item.queries) > budget:
                break
            merged: dict[str, SearchResult] = {}
            try:
                for query in item.queries:
                    for result in self.search.search(query, file_type=file_type):
                        current = merged.get(result.hash)
                        if current is None or result.sources > current.sources:
                            merged[result.hash] = result
            except ECError as exc:
                log.warning("wanted %s: search failed for %s: %s", source, item.label, exc)
                break
            budget -= len(item.queries)
            searched += 1
            hits += len(merged)
            with self._lock:
                item.results = list(merged.values())
                item.last_search = self.clock()
            log.info("wanted %s: %s -> %d results (%s)", source, item.label, len(merged), ", ".join(item.queries))
        log.info(
            "wanted %s: %d items wanted, %d searched now (%d hits), %d postponed",
            source,
            len(fresh),
            searched,
            hits,
            len(due) - searched,
        )
