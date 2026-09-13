import json
import os

import pytest

from amularr.config import Config
from amularr.ec import codes as C
from amularr.qbittorrent import QBittorrentAPI, parse_magnet
from amularr.search import SearchService
from amularr.state import State, btih_to_ed2k, ed2k_to_btih
from amularr.torznab import magnet_link

from .fakes import FakeEC, make_queued, make_result

ED2K = "0123456789ABCDEF0123456789ABCDEF"
BTIH = ED2K.lower() + "00000000"


@pytest.fixture
def env(tmp_path):
    ec = FakeEC()
    cfg = Config(
        ec_password="x",
        state_file=str(tmp_path / "state.json"),
        incoming_dir="/amule/incoming",
        local_incoming_dir=str(tmp_path / "incoming"),
        search_poll_interval=0,
        search_timeout=0.05,
    )
    os.makedirs(cfg.local_incoming_dir)
    state = State(cfg.state_file)
    search = SearchService(ec, cfg)
    api = QBittorrentAPI(ec, state, search, cfg)
    return api, ec, state, cfg


def call(api, route, method="GET", cookies=None, **params):
    query = params if method == "GET" else {}
    form = params if method == "POST" else {}
    return api.handle(method, "/api/v2/" + route, query, form, cookies or {})


def body(resp):
    return json.loads(resp.body)


def test_hash_mapping_roundtrip():
    assert btih_to_ed2k(ed2k_to_btih(ED2K)) == ED2K
    with pytest.raises(ValueError):
        btih_to_ed2k("ab" * 20)


def test_parse_magnet():
    m = parse_magnet(magnet_link(make_result(ED2K, "Dark 3x03 [WEBRip].avi", 123)))
    assert m.btih == BTIH
    assert m.name == "Dark 3x03 [WEBRip].avi"
    assert m.size == 123


def test_versions_and_preferences(env):
    api, *_ = env
    assert call(api, "app/webapiVersion").body == b"2.9.3"
    assert call(api, "app/version").body.startswith(b"v4")
    prefs = body(call(api, "app/preferences"))
    assert prefs["dht"] is True
    assert prefs["save_path"] == "/amule/incoming/"
    assert prefs["max_ratio_enabled"] is False


def test_auth_required_when_configured(env):
    api, ec, state, cfg = env
    cfg.qbt_username, cfg.qbt_password = "user", "pass"
    assert call(api, "torrents/info").status == 403
    assert call(api, "app/webapiVersion").status == 200
    bad = call(api, "auth/login", "POST", username="user", password="nope")
    assert bad.body == b"Fails."
    ok = call(api, "auth/login", "POST", username="user", password="pass")
    assert ok.body == b"Ok."
    sid = dict(ok.headers)["Set-Cookie"].split(";")[0].split("=", 1)[1]
    assert call(api, "torrents/info", cookies={"SID": sid}).status == 200


def test_add_magnet_then_track_progress(env):
    api, ec, state, cfg = env
    magnet = magnet_link(make_result(ED2K, "Dark 3x03 Adam y Eva.avi", 1000))
    resp = call(api, "torrents/add", "POST", urls=magnet, category="tv-sonarr")
    assert resp.body == b"Ok."
    assert ec.added_links == [f"ed2k://|file|Dark%203x03%20Adam%20y%20Eva.avi|1000|{ED2K}|/"]
    assert "tv-sonarr" in body(call(api, "torrents/categories"))

    items = body(call(api, "torrents/info", category="tv-sonarr"))
    assert len(items) == 1
    item = items[0]
    assert item["hash"] == BTIH
    assert item["name"] == "Dark 3x03 Adam y Eva.avi"
    assert item["state"] == "stalledDL"
    assert item["content_path"] == "/amule/incoming/Dark 3x03 Adam y Eva.avi"
    assert item["save_path"] == "/amule/incoming/"

    ec.queue[ED2K] = make_queued(ED2K, "Dark 3x03 Adam y Eva.avi", 1000, done=250, speed=50, sources_xfer=2)
    item = body(call(api, "torrents/info"))[0]
    assert item["state"] == "downloading"
    assert item["progress"] == 0.25
    assert item["eta"] == 15

    ec.queue[ED2K] = make_queued(ED2K, "Dark 3x03 Adam y Eva.avi", 1000, done=1000, status=C.PS_COMPLETE)
    item = body(call(api, "torrents/info"))[0]
    assert item["state"] == "pausedUP"
    assert item["progress"] == 1.0
    assert item["eta"] == 0
    assert item["completion_on"] > 0
    assert item["ratio_limit"] == 0.0

    files = body(call(api, "torrents/files", hash=BTIH))
    assert files[0]["name"] == "Dark 3x03 Adam y Eva.avi"
    props = body(call(api, "torrents/properties", hash=BTIH))
    assert props["save_path"] == "/amule/incoming/"

    # Completed downloads vanish from aMule's queue after a restart: the
    # bridge must keep reporting them as complete.
    del ec.queue[ED2K]
    assert body(call(api, "torrents/info"))[0]["state"] == "pausedUP"

    # Sonarr removes the item after import.
    resp = call(api, "torrents/delete", "POST", hashes=BTIH, deleteFiles="true")
    assert resp.status == 200
    assert body(call(api, "torrents/info")) == []
    assert ec.deleted == []


def test_add_without_dn_uses_search_cache(env):
    api, ec, state, cfg = env
    ec.results = [make_result(ED2K, "Some.Movie.2020.mkv", 5000)]
    api.search.search("some movie")
    resp = call(api, "torrents/add", "POST", urls=f"magnet:?xt=urn:btih:{BTIH}")
    assert resp.body == b"Ok."
    assert ec.added_links[0].endswith(f"|5000|{ED2K}|/")


def test_add_unknown_magnet_fails(env):
    api, *_ = env
    resp = call(api, "torrents/add", "POST", urls=f"magnet:?xt=urn:btih:{BTIH}")
    assert resp.body == b"Fails."
    assert resp.status == 415


def test_add_already_in_amule_is_ok(env):
    api, ec, *_ = env
    ec.fail_add = True
    magnet = magnet_link(make_result(ED2K, "x.mkv", 10))
    assert call(api, "torrents/add", "POST", urls=magnet).body == b"Ok."
    assert len(api.state.all()) == 1


def test_completed_detected_on_disk_when_missing_from_queue(env):
    api, ec, state, cfg = env
    magnet = magnet_link(make_result(ED2K, "done.mkv", 10))
    call(api, "torrents/add", "POST", urls=magnet)
    del ec.queue[ED2K]
    with open(os.path.join(cfg.local_incoming_dir, "done.mkv"), "wb") as fh:
        fh.write(b"0" * 10)
    item = body(call(api, "torrents/info"))[0]
    assert item["state"] == "pausedUP"
    call(api, "torrents/delete", "POST", hashes=BTIH, deleteFiles="true")
    assert not os.path.exists(os.path.join(cfg.local_incoming_dir, "done.mkv"))


def test_delete_cancels_unfinished_download(env):
    api, ec, *_ = env
    magnet = magnet_link(make_result(ED2K, "x.mkv", 10))
    call(api, "torrents/add", "POST", urls=magnet)
    call(api, "torrents/delete", "POST", hashes=BTIH)
    assert ec.deleted == [ED2K]


def test_pause_resume(env):
    api, ec, *_ = env
    magnet = magnet_link(make_result(ED2K, "x.mkv", 10))
    call(api, "torrents/add", "POST", urls=magnet)
    call(api, "torrents/pause", "POST", hashes=BTIH)
    assert ec.paused == [ED2K]
    assert body(call(api, "torrents/info"))[0]["state"] == "pausedDL"
    call(api, "torrents/resume", "POST", hashes=BTIH)
    assert ec.resumed == [ED2K]
    assert body(call(api, "torrents/info"))[0]["state"] == "stalledDL"


def test_state_persists_across_restart(env, tmp_path):
    api, ec, state, cfg = env
    magnet = magnet_link(make_result(ED2K, "x.mkv", 10))
    call(api, "torrents/add", "POST", urls=magnet, category="radarr")
    reloaded = State(cfg.state_file)
    assert reloaded.get(BTIH).name == "x.mkv"
    assert reloaded.categories == {"radarr": ""}


def test_set_category_requires_existing(env):
    api, *_ = env
    magnet = magnet_link(make_result(ED2K, "x.mkv", 10))
    call(api, "torrents/add", "POST", urls=magnet)
    assert call(api, "torrents/setCategory", "POST", hashes=BTIH, category="nope").status == 409
    call(api, "torrents/createCategory", "POST", category="tv-sonarr")
    assert call(api, "torrents/setCategory", "POST", hashes=BTIH, category="tv-sonarr").status == 200
    assert body(call(api, "torrents/info"))[0]["category"] == "tv-sonarr"


def test_unknown_endpoint_404(env):
    api, *_ = env
    assert call(api, "torrents/whatever").status == 404
