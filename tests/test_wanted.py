import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from amularr.config import Config
from amularr.search import SearchService
from amularr.torznab import TorznabAPI
from amularr.wanted import SOURCE_MOVIES, SOURCE_TV, WantedSearcher, radarr_items, series_titles, sonarr_items

from .fakes import FakeEC, make_result

H1 = "0123456789ABCDEF0123456789ABCDEF"
H2 = "FEDCBA9876543210FEDCBA9876543210"
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def sonarr_payload(*records):
    return {"page": 1, "pageSize": 200, "totalRecords": len(records), "records": list(records)}


def episode(eid, season, number, aired, title="Lanterns", alternate=()):
    return {
        "id": eid,
        "seriesId": 1,
        "seasonNumber": season,
        "episodeNumber": number,
        "airDateUtc": iso(aired),
        "monitored": True,
        "series": {"id": 1, "title": title, "alternateTitles": [dict(a) for a in alternate]},
    }


def movie(mid, title, year, alternate=(), **dates):
    return {
        "id": mid,
        "title": title,
        "originalTitle": title,
        "year": year,
        "alternateTitles": [dict(a) for a in alternate],
        **{k: iso(v) for k, v in dates.items()},
    }


class FakeFetch:
    def __init__(self, payloads):
        self.payloads = payloads  # url substring -> payload
        self.calls = []

    def __call__(self, url, api_key, params):
        self.calls.append((url, api_key, params))
        for needle, payload in self.payloads.items():
            if needle in url:
                return payload
        raise AssertionError(f"unexpected url {url}")


def make_cfg(**overrides):
    return Config(ec_password="x", search_poll_interval=0, search_timeout=0.05, state_file=None, **overrides)


# ----------------------------------------------------------------------


def test_series_titles_follow_scene_seasons():
    series = {
        "title": "Lanterns",
        "alternateTitles": [
            {"title": "Linternas", "seasonNumber": -1, "sceneSeasonNumber": None},
            {"title": "Lanterns Season Two", "seasonNumber": 2, "sceneSeasonNumber": None},
            {"title": "Lanterns Part 1", "seasonNumber": 1, "sceneSeasonNumber": 5},
        ],
    }
    assert series_titles(series, 1, 5) == [("Lanterns", 1), ("Linternas", 1), ("Lanterns Part 1", 5)]
    assert series_titles(series, 2, 5) == [("Lanterns", 2), ("Linternas", 2), ("Lanterns Season Two", 2)]
    assert series_titles(series, 1, 2) == [("Lanterns", 1), ("Linternas", 1)]


def test_sonarr_items_window_and_queries():
    cfg = make_cfg(sonarr_url="http://sonarr:8989/", sonarr_api_key="k", wanted_days=7)
    fetch = FakeFetch(
        {
            "/api/v3/wanted/missing": sonarr_payload(
                episode(5, 1, 5, NOW - timedelta(hours=10), alternate=[{"title": "Linternas", "seasonNumber": -1}]),
                episode(6, 1, 6, NOW + timedelta(days=6)),  # not aired yet
                episode(1, 1, 1, NOW - timedelta(days=30)),  # too old
                episode(9, 0, 3, NOW - timedelta(days=1)),  # special
            )
        }
    )
    items = list(sonarr_items(cfg, fetch, NOW))
    assert [i.key for i in items] == ["tv:5"]
    assert items[0].label == "Lanterns S01E05"
    assert items[0].queries == ["Lanterns S01E05", "Lanterns 1x05", "Linternas S01E05", "Linternas 1x05"]
    url, key, params = fetch.calls[0]
    assert url == "http://sonarr:8989/api/v3/wanted/missing"
    assert key == "k"
    assert params["includeSeries"] == "true" and params["monitored"] == "true"


def test_radarr_items_use_release_dates_and_languages():
    cfg = make_cfg(radarr_url="http://radarr:7878", radarr_api_key="k", wanted_days=7)
    fetch = FakeFetch(
        {
            "/api/v3/wanted/missing": {
                "records": [
                    movie(
                        1,
                        "Prey",
                        2022,
                        alternate=[
                            {"title": "Predator: La presa", "language": {"name": "Spanish"}},
                            {"title": "Predator: Beute", "language": {"name": "German"}},
                        ],
                        digitalRelease=NOW - timedelta(days=2),
                        inCinemas=NOW - timedelta(days=60),
                    ),
                    movie(2, "Old", 2019, digitalRelease=NOW - timedelta(days=400), added=NOW - timedelta(days=100)),
                    movie(3, "Just Added", 2010, digitalRelease=NOW - timedelta(days=400), added=NOW - timedelta(days=1)),
                ]
            }
        }
    )
    items = list(radarr_items(cfg, fetch, NOW))
    assert [i.key for i in items] == ["movies:1", "movies:3"]
    assert items[0].queries == ["Prey 2022", "Predator La presa 2022"]
    assert items[1].queries == ["Just Added 2010"]


def test_searcher_refreshes_and_feeds_rss():
    ec = FakeEC()
    ec.results = [make_result(H1, "Linternas 1x05. Luces fuera [HD-720p][Spanish].mkv", 900_000_000, sources=6)]
    cfg = make_cfg(sonarr_url="http://sonarr:8989", sonarr_api_key="k", wanted_interval=900, wanted_research_interval=3600)
    fetch = FakeFetch({"/wanted/missing": sonarr_payload(episode(5, 1, 5, NOW - timedelta(hours=1)))})
    ticks = [1000.0]
    search = SearchService(ec, cfg)
    wanted = WantedSearcher(search, cfg, fetch=fetch, clock=lambda: ticks[0])
    api = TorznabAPI(search, cfg, wanted)

    # An RSS request for TV kicks a refresh; the hits show up in the feed.
    assert wanted.maybe_refresh([SOURCE_TV], wait=True) is True
    assert [s[0] for s in ec.searches] == ["Lanterns S01E05", "Lanterns 1x05"]
    resp = api.handle({"t": "search", "cat": "5000,5030,5040,5045"})
    root = ET.fromstring(resp.body)
    titles = [i.findtext("title") for i in root.iter("item")]
    assert titles == ["Linternas 1x05. Luces fuera [HD-720p][Spanish].mkv"]

    # Within the interval nothing is refreshed again, not even after the
    # search cache expired: the wanted results are kept on their own.
    ticks[0] += 800
    assert wanted.maybe_refresh([SOURCE_TV], wait=True) is False
    assert len(fetch.calls) == 1
    assert len(ec.searches) == 2

    # Past the interval the list is re-read but the item is not searched
    # again until the research interval elapses.
    ticks[0] += 200
    assert wanted.maybe_refresh([SOURCE_TV], wait=True) is True
    assert len(fetch.calls) == 2
    assert len(ec.searches) == 2

    # Once Sonarr no longer wants the episode, its results leave the feed.
    fetch.payloads["/wanted/missing"] = sonarr_payload()
    ticks[0] += 1000
    assert wanted.maybe_refresh([SOURCE_TV], wait=True) is True
    assert wanted.results() == []


def test_searcher_disabled_without_credentials():
    cfg = make_cfg()
    wanted = WantedSearcher(SearchService(FakeEC(), cfg), cfg, fetch=FakeFetch({}))
    assert wanted.sources() == []
    assert wanted.maybe_refresh(wait=True) is False
    assert wanted.maybe_refresh([SOURCE_MOVIES], wait=True) is False


def test_searcher_respects_search_budget():
    ec = FakeEC()
    cfg = make_cfg(sonarr_url="http://sonarr:8989", sonarr_api_key="k", wanted_max_searches=3)
    fetch = FakeFetch(
        {
            "/wanted/missing": sonarr_payload(
                episode(1, 1, 1, NOW - timedelta(hours=1)),
                episode(2, 1, 2, NOW - timedelta(hours=2)),
            )
        }
    )
    wanted = WantedSearcher(SearchService(ec, cfg), cfg, fetch=fetch)
    wanted.maybe_refresh([SOURCE_TV], wait=True)
    assert len(ec.searches) == 2  # one item = 2 queries; the second does not fit in the budget of 3


def test_wanted_fetch_failure_is_logged_not_raised():
    def broken(url, api_key, params):
        raise OSError("connection refused")

    cfg = make_cfg(sonarr_url="http://sonarr:8989", sonarr_api_key="k")
    wanted = WantedSearcher(SearchService(FakeEC(), cfg), cfg, fetch=broken)
    assert wanted.maybe_refresh([SOURCE_TV], wait=True) is True
    assert wanted.results() == []


def test_pubdate_is_first_sighting():
    ec = FakeEC()
    ec.results = [make_result(H2, "Dark.S03E03.720p.WEB.x264.mkv", 800_000_000, sources=12)]
    cfg = make_cfg()
    search = SearchService(ec, cfg)
    api = TorznabAPI(search, cfg)
    first = ET.fromstring(api.handle({"t": "search", "q": "Dark S03E03"}).body).find(".//item/pubDate").text
    search._first_seen[H2] -= 3600  # pretend an hour went by
    again = ET.fromstring(api.handle({"t": "search", "q": "Dark S03E03"}).body).find(".//item/pubDate").text
    assert again != first
    assert search.first_seen(H2) is not None
