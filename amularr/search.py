"""Serialized, cached access to aMule searches.

aMule keeps a single result list per core: starting a new search wipes
the previous one. The service therefore runs searches one at a time,
polls until the core reports completion, and caches results per query
so that repeated *arr requests do not hit the network again.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from .config import Config
from .ec import ECClient, ECError, SearchResult

log = logging.getLogger(__name__)

PROGRESS_DONE = 100
PROGRESS_IDLE = 0xFFFF
FIRST_SEEN_RETENTION = 30 * 86400  # seconds a hash keeps its first-seen date


@dataclass
class _CacheEntry:
    timestamp: float
    results: list[SearchResult]


class SearchService:
    def __init__(self, ec: ECClient, config: Config):
        self.ec = ec
        self.config = config
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str, int], _CacheEntry] = {}
        self._known: dict[str, SearchResult] = {}  # ed2k hash -> last seen result
        self._first_seen: dict[str, float] = {}  # ed2k hash -> epoch seconds of the first sighting
        self._known_lock = threading.Lock()

    # ------------------------------------------------------------------

    def search(self, text: str, file_type: str = "", min_size: int = 0) -> list[SearchResult]:
        text = " ".join(text.split())
        if not text:
            return []
        key = (text.lower(), file_type, min_size)
        cached = self._get_cached(key)
        if cached is not None:
            return cached
        with self._lock:
            cached = self._get_cached(key)
            if cached is not None:
                return cached
            results = self._run(text, file_type, min_size)
            self._cache[key] = _CacheEntry(time.monotonic(), results)
            self._prune_cache()
        self._remember(results)
        return results

    def _remember(self, results: list[SearchResult]) -> None:
        now = time.time()
        with self._known_lock:
            for result in results:
                self._known[result.hash] = result
                self._first_seen.setdefault(result.hash, now)
            if len(self._first_seen) > 20000:
                for key in [k for k, t in self._first_seen.items() if now - t > FIRST_SEEN_RETENTION]:
                    del self._first_seen[key]

    def find_known(self, ed2k_hash: str) -> SearchResult | None:
        with self._known_lock:
            return self._known.get(ed2k_hash.upper())

    def first_seen(self, ed2k_hash: str) -> float | None:
        """Epoch seconds of the first time a hash showed up in a search;
        used as the feed's pubDate so RSS consumers see a stable date."""
        with self._known_lock:
            return self._first_seen.get(ed2k_hash.upper())

    def recent(self) -> list[SearchResult]:
        """Union of every cached result, newest searches first."""
        seen: set[str] = set()
        out: list[SearchResult] = []
        with self._lock:
            entries = sorted(self._cache.values(), key=lambda e: -e.timestamp)
        for entry in entries:
            for result in entry.results:
                if result.hash not in seen:
                    seen.add(result.hash)
                    out.append(result)
        return out

    # ------------------------------------------------------------------

    def _get_cached(self, key) -> list[SearchResult] | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        if time.monotonic() - entry.timestamp > self.config.search_cache_ttl:
            return None
        return entry.results

    def _prune_cache(self) -> None:
        now = time.monotonic()
        for key in [k for k, e in self._cache.items() if now - e.timestamp > self.config.search_cache_ttl]:
            del self._cache[key]

    def _run(self, text: str, file_type: str, min_size: int) -> list[SearchResult]:
        cfg = self.config
        started = time.monotonic()
        try:
            status = self.ec.search_start(text, cfg.search_type, file_type=file_type, min_size=min_size)
        except ECError as exc:
            log.error("search %r refused by aMule: %s", text, exc)
            return []
        log.info("search %r started: %s", text, status)
        deadline = started + cfg.search_timeout
        progress = 0
        while time.monotonic() < deadline:
            time.sleep(cfg.search_poll_interval)
            progress = self.ec.search_progress()
            if progress >= PROGRESS_DONE:
                break
        results = self.ec.search_results()
        if progress < PROGRESS_DONE:
            # Give the core one last moment to flush what it has.
            time.sleep(cfg.search_poll_interval)
            results = self.ec.search_results()
        try:
            self.ec.search_stop()
        except ECError:
            pass
        log.info(
            "search %r finished in %.1fs (progress=%s, results=%d)",
            text,
            time.monotonic() - started,
            "idle" if progress == PROGRESS_IDLE else progress,
            len(results),
        )
        return results
