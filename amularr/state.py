"""Persistent bookkeeping of downloads handed to aMule by the *arr apps.

aMule forgets finished files from its download list (and loses the
mapping between the fake torrent hash and the ed2k hash), so the bridge
keeps its own record of every download it was asked to manage.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field

log = logging.getLogger(__name__)

BTIH_SUFFIX = "00000000"


def ed2k_to_btih(ed2k_hash: str) -> str:
    """Fake 40-hex BitTorrent info hash carrying the 32-hex ed2k hash."""
    ed2k_hash = ed2k_hash.lower()
    if len(ed2k_hash) != 32:
        raise ValueError("ed2k hash must be 32 hex chars")
    return ed2k_hash + BTIH_SUFFIX


def btih_to_ed2k(btih: str) -> str:
    btih = btih.lower()
    if len(btih) != 40 or not btih.endswith(BTIH_SUFFIX):
        raise ValueError("not an amularr info hash")
    return btih[:32].upper()


@dataclass
class Download:
    hash: str  # fake btih, lowercase
    ed2k: str  # ed2k hash, uppercase
    name: str
    size: int
    category: str = ""
    added_on: int = field(default_factory=lambda: int(time.time()))
    completed_on: int = 0
    paused: bool = False

    @property
    def ed2k_link(self) -> str:
        from urllib.parse import quote

        return f"ed2k://|file|{quote(self.name, safe='')}|{self.size}|{self.ed2k}|/"


class State:
    def __init__(self, path: str | None):
        self.path = path
        self._lock = threading.RLock()
        self.downloads: dict[str, Download] = {}
        self.categories: dict[str, str] = {}  # name -> save path ("" = default)
        self._load()

    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError) as exc:
            log.error("cannot read state file %s: %s", self.path, exc)
            return
        for item in raw.get("downloads", []):
            try:
                dl = Download(**item)
            except TypeError as exc:
                log.warning("skipping malformed download record %r: %s", item, exc)
                continue
            self.downloads[dl.hash] = dl
        self.categories = dict(raw.get("categories", {}))
        log.info("loaded %d downloads and %d categories from %s", len(self.downloads), len(self.categories), self.path)

    def save(self) -> None:
        if not self.path:
            return
        with self._lock:
            data = {
                "downloads": [asdict(d) for d in self.downloads.values()],
                "categories": self.categories,
            }
            directory = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".amularr-", suffix=".json", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=2, ensure_ascii=False)
                os.replace(tmp, self.path)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    # ------------------------------------------------------------------

    def add(self, download: Download) -> Download:
        with self._lock:
            existing = self.downloads.get(download.hash)
            if existing is not None:
                if download.category:
                    existing.category = download.category
                self.save()
                return existing
            self.downloads[download.hash] = download
            if download.category and download.category not in self.categories:
                self.categories[download.category] = ""
            self.save()
            return download

    def get(self, btih: str) -> Download | None:
        return self.downloads.get(btih.lower())

    def by_ed2k(self, ed2k: str) -> Download | None:
        ed2k = ed2k.upper()
        for dl in self.downloads.values():
            if dl.ed2k == ed2k:
                return dl
        return None

    def remove(self, btih: str) -> Download | None:
        with self._lock:
            dl = self.downloads.pop(btih.lower(), None)
            if dl is not None:
                self.save()
            return dl

    def update(self, download: Download) -> None:
        with self._lock:
            self.downloads[download.hash] = download
            self.save()

    def all(self) -> list[Download]:
        with self._lock:
            return list(self.downloads.values())

    def add_category(self, name: str, save_path: str = "") -> None:
        with self._lock:
            self.categories[name] = save_path
            self.save()

    def remove_category(self, name: str) -> None:
        with self._lock:
            self.categories.pop(name, None)
            for dl in self.downloads.values():
                if dl.category == name:
                    dl.category = ""
            self.save()
