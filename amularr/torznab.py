"""Torznab indexer facade over aMule searches.

Every ed2k result is exposed as a fake torrent: the ``ed2k`` MD4 hash is
embedded in a 40-hex BitTorrent info hash (see ``state.ed2k_to_btih``)
and offered through a magnet link. The qBittorrent emulation in
``qbittorrent.py`` understands those magnets and turns them back into
ed2k downloads.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import formatdate
from urllib.parse import quote

from .config import Config
from .ec import SearchResult
from .search import SearchService
from .state import ed2k_to_btih
from .wanted import SOURCE_MOVIES, SOURCE_TV, WantedSearcher
from .web import Response

log = logging.getLogger(__name__)

TORZNAB_NS = "http://torznab.com/schemas/2015/feed"
ATOM_NS = "http://www.w3.org/2005/Atom"
ET.register_namespace("torznab", TORZNAB_NS)
ET.register_namespace("atom", ATOM_NS)

CAT_MOVIES, CAT_MOVIES_SD, CAT_MOVIES_HD, CAT_MOVIES_UHD = 2000, 2030, 2040, 2045
CAT_TV, CAT_TV_SD, CAT_TV_HD, CAT_TV_UHD = 5000, 5030, 5040, 5045
CAT_OTHER, CAT_OTHER_MISC = 8000, 8010

CATEGORIES: dict[int, tuple[str, dict[int, str]]] = {
    CAT_MOVIES: ("Movies", {CAT_MOVIES_SD: "Movies/SD", CAT_MOVIES_HD: "Movies/HD", CAT_MOVIES_UHD: "Movies/UHD"}),
    CAT_TV: ("TV", {CAT_TV_SD: "TV/SD", CAT_TV_HD: "TV/HD", CAT_TV_UHD: "TV/UHD"}),
    CAT_OTHER: ("Other", {CAT_OTHER_MISC: "Other/Misc"}),
}

DOMAIN_TV = "tv"
DOMAIN_MOVIES = "movies"
DOMAIN_OTHER = "other"

_RE_UHD = re.compile(r"(?i)\b(2160p|4k|uhd)\b")
_RE_HD = re.compile(r"(?i)\b(720p|1080p|1080i)\b")
_RE_EPISODE = re.compile(
    r"(?i)(\bS\d{1,2}\s?E\d{1,3}\b|\b\d{1,2}x\d{2,3}\b|\bS\d{2}\b|\b(season|temporada|stagione|saison|staffel)\s*\d+|\bcap[ií]tulo\s*\d+)"
)
_RE_CLEAN = re.compile(r"[^\w\s\-]", re.UNICODE)


def magnet_link(result: SearchResult) -> str:
    return f"magnet:?xt=urn:btih:{ed2k_to_btih(result.hash)}&dn={quote(result.name, safe='')}&xl={result.size}"


def clean_query(text: str) -> str:
    text = _RE_CLEAN.sub(" ", text.replace("'", "").replace("’", ""))
    return " ".join(text.split())


def build_queries(kind: str, q: str, season: str | None, ep: str | None, year: str | None) -> list[str]:
    """Return the ed2k keyword searches to run for one Torznab request."""
    q = clean_query(q or "")
    if not q:
        return []
    if kind == "tvsearch":
        s = int(season) if season and season.isdigit() else None
        e = int(ep) if ep and ep.isdigit() else None
        if s is not None and e is not None:
            return [f"{q} S{s:02d}E{e:02d}", f"{q} {s}x{e:02d}"]
        if s is not None:
            return [f"{q} S{s:02d}", f"{q} temporada {s}"]
        return [q]
    if kind == "movie":
        if year and year.isdigit():
            return [f"{q} {year}", q]
        return [q]
    return [q]


def classify(name: str, domain: str) -> tuple[int, int]:
    """Return (parent category, sub category) for a result."""
    if domain == DOMAIN_TV or (domain != DOMAIN_OTHER and _RE_EPISODE.search(name)):
        if _RE_UHD.search(name):
            return CAT_TV, CAT_TV_UHD
        if _RE_HD.search(name):
            return CAT_TV, CAT_TV_HD
        return CAT_TV, CAT_TV_SD
    if domain == DOMAIN_OTHER:
        return CAT_OTHER, CAT_OTHER_MISC
    if _RE_UHD.search(name):
        return CAT_MOVIES, CAT_MOVIES_UHD
    if _RE_HD.search(name):
        return CAT_MOVIES, CAT_MOVIES_HD
    return CAT_MOVIES, CAT_MOVIES_SD


def parse_cats(value: str | None) -> set[int]:
    cats: set[int] = set()
    for piece in (value or "").split(","):
        piece = piece.strip()
        if piece.isdigit():
            cats.add(int(piece))
    return cats


def domain_for(kind: str, cats: set[int]) -> str:
    if kind == "tvsearch":
        return DOMAIN_TV
    if kind == "movie":
        return DOMAIN_MOVIES
    parents = {c - c % 1000 for c in cats}
    if parents == {CAT_TV}:
        return DOMAIN_TV
    if parents == {CAT_MOVIES}:
        return DOMAIN_MOVIES
    if parents == {CAT_OTHER}:
        return DOMAIN_OTHER
    return "auto"


def cat_allowed(parent: int, sub: int, cats: set[int]) -> bool:
    if not cats:
        return True
    return parent in cats or sub in cats


@dataclass
class Item:
    result: SearchResult
    parent: int
    sub: int
    seen: float | None = None  # epoch seconds of the first sighting (feed pubDate)


# ----------------------------------------------------------------------
# XML rendering
# ----------------------------------------------------------------------


def error_xml(code: int, description: str) -> bytes:
    root = ET.Element("error", code=str(code), description=description)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def caps_xml(config: Config) -> bytes:
    caps = ET.Element("caps")
    ET.SubElement(caps, "server", title=config.indexer_name, version="1.0")
    ET.SubElement(caps, "limits", max=str(config.max_results), default=str(min(100, config.max_results)))
    searching = ET.SubElement(caps, "searching")
    ET.SubElement(searching, "search", available="yes", supportedParams="q")
    ET.SubElement(searching, "tv-search", available="yes", supportedParams="q,season,ep")
    ET.SubElement(searching, "movie-search", available="yes", supportedParams="q,year")
    ET.SubElement(searching, "music-search", available="no", supportedParams="q")
    ET.SubElement(searching, "audio-search", available="no", supportedParams="q")
    ET.SubElement(searching, "book-search", available="no", supportedParams="q")
    categories = ET.SubElement(caps, "categories")
    for parent_id, (parent_name, subs) in CATEGORIES.items():
        parent = ET.SubElement(categories, "category", id=str(parent_id), name=parent_name)
        for sub_id, sub_name in subs.items():
            ET.SubElement(parent, "subcat", id=str(sub_id), name=sub_name)
    return ET.tostring(caps, encoding="utf-8", xml_declaration=True)


def results_xml(items: list[Item], config: Config, offset: int, total: int) -> bytes:
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, f"{{{ATOM_NS}}}link", rel="self", type="application/rss+xml")
    ET.SubElement(channel, "title").text = config.indexer_name
    ET.SubElement(channel, "description").text = "aMule (ed2k/Kad) results exposed as Torznab"
    ET.SubElement(channel, "link").text = "https://github.com/manuel-alcocer/amularr"
    ET.SubElement(channel, "language").text = "en-US"
    ET.SubElement(channel, f"{{{TORZNAB_NS}}}response", offset=str(offset), total=str(total))
    now = formatdate(usegmt=True)
    for item in items:
        r = item.result
        magnet = magnet_link(r)
        node = ET.SubElement(channel, "item")
        ET.SubElement(node, "title").text = r.name
        ET.SubElement(node, "guid", isPermaLink="false").text = r.ed2k_link
        ET.SubElement(node, "link").text = magnet
        ET.SubElement(node, "pubDate").text = formatdate(item.seen, usegmt=True) if item.seen else now
        ET.SubElement(node, "size").text = str(r.size)
        ET.SubElement(node, "category").text = str(item.sub)
        ET.SubElement(node, "enclosure", url=magnet, length=str(r.size), type="application/x-bittorrent")
        attrs = [
            ("category", str(item.parent)),
            ("category", str(item.sub)),
            ("size", str(r.size)),
            ("seeders", str(r.sources)),
            ("peers", str(r.sources)),
            ("leechers", "0"),
            ("infohash", ed2k_to_btih(r.hash)),
            ("magneturl", magnet),
            ("downloadvolumefactor", "0"),
            ("uploadvolumefactor", "0"),
        ]
        for name, value in attrs:
            ET.SubElement(node, f"{{{TORZNAB_NS}}}attr", name=name, value=value)
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


# ----------------------------------------------------------------------
# Request handling
# ----------------------------------------------------------------------


class TorznabAPI:
    def __init__(self, search: SearchService, config: Config, wanted: WantedSearcher | None = None):
        self.search = search
        self.config = config
        self.wanted = wanted or WantedSearcher(search, config)

    def handle(self, params: dict[str, str]) -> Response:
        cfg = self.config
        if cfg.api_key and params.get("apikey") != cfg.api_key:
            return Response.xml(error_xml(100, "Incorrect user credentials"), 401)
        kind = (params.get("t") or "").lower()
        if kind == "caps":
            return Response.xml(caps_xml(cfg))
        if kind not in ("search", "tvsearch", "movie"):
            return Response.xml(error_xml(202, "No such function"), 400)

        cats = parse_cats(params.get("cat"))
        domain = domain_for(kind, cats)
        limit = _int(params.get("limit"), min(100, cfg.max_results))
        limit = max(1, min(limit, cfg.max_results))
        offset = max(0, _int(params.get("offset"), 0))

        queries = build_queries(kind, params.get("q", ""), params.get("season"), params.get("ep"), params.get("year"))
        if queries:
            file_type = cfg.file_type if domain in (DOMAIN_TV, DOMAIN_MOVIES, "auto") else ""
            results = self._run_queries(queries, file_type)
        else:
            # RSS-style request without keywords. ed2k has no "what's new",
            # so serve what the wanted-list searcher found for the *arr apps
            # (kicking a refresh in the background), then whatever was
            # searched recently, or the configured feed query so the feed is
            # never empty.
            sources = {DOMAIN_TV: [SOURCE_TV], DOMAIN_MOVIES: [SOURCE_MOVIES]}.get(domain)
            self.wanted.maybe_refresh(sources)
            results = self._merge(self.wanted.results(sources), self.search.recent())
            if not results and cfg.rss_query:
                file_type = cfg.file_type if domain in (DOMAIN_TV, DOMAIN_MOVIES, "auto") else ""
                results = self.search.search(cfg.rss_query, file_type=file_type)

        items = self._filter(results, domain, cats)
        items.sort(key=lambda i: (-i.result.sources, i.result.name.lower()))
        total = len(items)
        page = items[offset : offset + limit]
        log.info("torznab %s q=%r cats=%s -> %d results (%d returned)", kind, params.get("q", ""), sorted(cats), total, len(page))
        return Response.xml(results_xml(page, cfg, offset, total))

    def _run_queries(self, queries: list[str], file_type: str) -> list[SearchResult]:
        return self._merge(*(self.search.search(query, file_type=file_type) for query in queries))

    @staticmethod
    def _merge(*result_sets: list[SearchResult]) -> list[SearchResult]:
        """Union by ed2k hash, keeping the sighting with most sources."""
        merged: dict[str, SearchResult] = {}
        for results in result_sets:
            for result in results:
                current = merged.get(result.hash)
                if current is None or result.sources > current.sources:
                    merged[result.hash] = result
        return list(merged.values())

    def _filter(self, results: list[SearchResult], domain: str, cats: set[int]) -> list[Item]:
        cfg = self.config
        video_only = domain in (DOMAIN_TV, DOMAIN_MOVIES)
        allowed_ext = set(cfg.video_extensions)
        items: list[Item] = []
        for r in results:
            if not r.hash or r.size <= 0:
                continue
            if r.sources < cfg.min_sources:
                continue
            ext = r.name.rsplit(".", 1)[-1].lower() if "." in r.name else ""
            if video_only and ext not in allowed_ext:
                continue
            parent, sub = classify(r.name, domain)
            if not cat_allowed(parent, sub, cats):
                continue
            items.append(Item(r, parent, sub, self.search.first_seen(r.hash)))
        return items


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default
