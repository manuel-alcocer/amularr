import xml.etree.ElementTree as ET

from amularr import torznab
from amularr.config import Config
from amularr.search import SearchService
from amularr.torznab import TorznabAPI

from .fakes import FakeEC, make_result

NS = {"torznab": torznab.TORZNAB_NS}
H1 = "0123456789ABCDEF0123456789ABCDEF"
H2 = "FEDCBA9876543210FEDCBA9876543210"
H3 = "00000000000000000000000000000001"


def make_api(results=None, **overrides):
    ec = FakeEC()
    ec.results = results or []
    cfg = Config(ec_password="x", search_poll_interval=0, search_timeout=0.05, state_file=None, **overrides)
    api = TorznabAPI(SearchService(ec, cfg), cfg)
    return api, ec


def test_caps():
    api, _ = make_api()
    resp = api.handle({"t": "caps"})
    assert resp.status == 200
    root = ET.fromstring(resp.body)
    assert root.tag == "caps"
    assert root.find("searching/tv-search").get("supportedParams") == "q,season,ep"
    ids = {c.get("id") for c in root.iter("category")}
    assert {"2000", "5000", "8000"} <= ids


def test_apikey_enforced():
    api, _ = make_api(api_key="secret")
    assert api.handle({"t": "caps"}).status == 401
    assert api.handle({"t": "caps", "apikey": "secret"}).status == 200


def test_build_queries():
    assert torznab.build_queries("tvsearch", "Dark", "3", "3", None) == ["Dark S03E03", "Dark 3x03"]
    assert torznab.build_queries("tvsearch", "Dark", "3", None, None) == ["Dark S03", "Dark 3x"]
    assert torznab.build_queries("tvsearch", "The Office (US)", None, None, None) == ["The Office US"]
    assert torznab.build_queries("movie", "Blade Runner", None, None, "1982") == ["Blade Runner 1982", "Blade Runner"]
    assert torznab.build_queries("search", "  ", None, None, None) == []
    assert torznab.build_queries("search", "Marvel's Agents: S.H.I.E.L.D.", None, None, None) == ["Marvels Agents S H I E L D"]


def test_tvsearch_merges_queries_and_filters_video():
    results = [
        make_result(H1, "Dark 3x03 Adam y Eva [WEBRip 1080p][GrupoTS].mkv", 900_000_000, sources=8),
        make_result(H2, "Dark.S03E03.720p.WEB.x264.mkv", 800_000_000, sources=12),
        make_result(H3, "Dark S03E03 subtitles.srt", 50_000, sources=30),
    ]
    api, ec = make_api(results)
    resp = api.handle({"t": "tvsearch", "q": "Dark", "season": "3", "ep": "3", "cat": "5000,5040"})
    assert resp.status == 200
    assert [s[0] for s in ec.searches] == ["Dark S03E03", "Dark 3x03"]
    assert ec.searches[0][2] == "Video"
    root = ET.fromstring(resp.body)
    items = root.findall("channel/item")
    titles = [i.findtext("title") for i in items]
    assert titles == ["Dark.S03E03.720p.WEB.x264.mkv", "Dark 3x03 Adam y Eva [WEBRip 1080p][GrupoTS].mkv"]
    first = items[0]
    assert first.find("link").text.startswith("magnet:?xt=urn:btih:" + H2.lower() + "00000000")
    attrs = {a.get("name"): a.get("value") for a in first.findall("torznab:attr", NS)}
    assert attrs["seeders"] == "12"
    assert attrs["infohash"] == H2.lower() + "00000000"
    assert attrs["magneturl"] == first.find("link").text
    assert first.find("enclosure").get("url") == attrs["magneturl"]
    assert "5040" in {a.get("value") for a in first.findall("torznab:attr[@name='category']", NS)}
    assert root.find("channel/torznab:response", NS).get("total") == "2"


def test_search_uses_cache_for_repeated_queries():
    api, ec = make_api([make_result(H1, "a.mkv", 10)])
    api.handle({"t": "search", "q": "foo"})
    api.handle({"t": "search", "q": "foo"})
    assert len(ec.searches) == 1


def test_empty_answers_are_cached_only_briefly(monkeypatch):
    from amularr import search as search_module

    api, ec = make_api([])
    api.handle({"t": "search", "q": "foo"})
    api.handle({"t": "search", "q": "foo"})
    assert len(ec.searches) == 1  # within EMPTY_CACHE_TTL the empty answer is reused
    monkeypatch.setattr(search_module, "EMPTY_CACHE_TTL", 0.0)
    api.handle({"t": "search", "q": "foo"})
    assert len(ec.searches) == 2  # an empty answer expires long before search_cache_ttl


def test_rss_request_without_query_returns_recent_results():
    api, ec = make_api([make_result(H1, "Some.Movie.2020.1080p.mkv", 10)])
    api.handle({"t": "movie", "q": "Some Movie"})
    resp = api.handle({"t": "search", "cat": "2000"})
    root = ET.fromstring(resp.body)
    assert [i.findtext("title") for i in root.findall("channel/item")] == ["Some.Movie.2020.1080p.mkv"]
    assert len(ec.searches) == 1  # "Some Movie" only; the rss call ran no search


def test_results_carry_volume_factors_for_indexer_flags():
    api, _ = make_api([make_result(H1, "Some.Movie.2020.1080p.mkv", 10)])
    root = ET.fromstring(api.handle({"t": "movie", "q": "Some Movie"}).body)
    attrs = {a.get("name"): a.get("value") for a in root.findall("channel/item/torznab:attr", NS)}
    assert attrs["downloadvolumefactor"] == "0.25"  # Sonarr/Radarr flag "Freeleech75"
    assert attrs["uploadvolumefactor"] == "1"
    api, _ = make_api([make_result(H1, "Some.Movie.2020.1080p.mkv", 10)], download_volume_factor="0", upload_volume_factor="2")
    root = ET.fromstring(api.handle({"t": "movie", "q": "Some Movie"}).body)
    attrs = {a.get("name"): a.get("value") for a in root.findall("channel/item/torznab:attr", NS)}
    assert (attrs["downloadvolumefactor"], attrs["uploadvolumefactor"]) == ("0", "2")


def test_rss_request_with_empty_cache_runs_feed_query():
    api, ec = make_api([make_result(H1, "Some.Movie.2020.1080p.mkv", 10)])
    resp = api.handle({"t": "search", "extended": "1"})
    root = ET.fromstring(resp.body)
    assert len(root.findall("channel/item")) == 1
    assert [s[0] for s in ec.searches] == ["1080p"]
    api.handle({"t": "search"})
    assert len(ec.searches) == 1  # served from cache


def test_min_sources_and_category_filter():
    results = [
        make_result(H1, "Movie.2020.2160p.mkv", 10, sources=1),
        make_result(H2, "Show.S01E02.mkv", 10, sources=5),
    ]
    api, _ = make_api(results, min_sources=2)
    resp = api.handle({"t": "search", "q": "x"})
    root = ET.fromstring(resp.body)
    items = root.findall("channel/item")
    assert [i.findtext("title") for i in items] == ["Show.S01E02.mkv"]
    assert items[0].findtext("category") == "5030"
    resp = api.handle({"t": "search", "q": "x", "cat": "2000"})
    assert ET.fromstring(resp.body).findall("channel/item") == []


def test_classify():
    assert torznab.classify("Show.S01E02.2160p.mkv", "auto") == (5000, 5045)
    assert torznab.classify("Movie.1982.720p.mkv", "auto") == (2000, 2040)
    assert torznab.classify("Movie.1982.DVDRip.avi", "movies") == (2000, 2030)
    assert torznab.classify("anything.pdf", "other") == (8000, 8010)


def test_pagination():
    results = [make_result(f"{i:032X}", f"f{i}.mkv", 10, sources=10 - i) for i in range(5)]
    api, _ = make_api(results)
    resp = api.handle({"t": "search", "q": "x", "limit": "2", "offset": "1"})
    root = ET.fromstring(resp.body)
    assert [i.findtext("title") for i in root.findall("channel/item")] == ["f1.mkv", "f2.mkv"]
    assert root.find("channel/torznab:response", NS).get("offset") == "1"
