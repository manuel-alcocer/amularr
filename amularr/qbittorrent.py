"""Emulation of the qBittorrent Web API (v2) on top of aMule.

Sonarr and Radarr talk to this as if it were qBittorrent. Magnet links
produced by ``torznab.py`` are converted back into ed2k links and handed
to aMule; the download list is rebuilt from aMule's queue plus the
bridge's own state file.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

from .config import Config
from .ec import ECClient, ECError, QueuedFile
from .ec import codes as C
from .search import SearchService
from .state import Download, State, btih_to_ed2k
from .web import Response

log = logging.getLogger(__name__)

API_VERSION = "2.9.3"
APP_VERSION = "v4.6.7"
ETA_INFINITE = 8640000

STATE_DOWNLOADING = "downloading"
STATE_STALLED = "stalledDL"
STATE_PAUSED_DL = "pausedDL"
STATE_QUEUED_DL = "queuedDL"
STATE_CHECKING_DL = "checkingDL"
STATE_COMPLETED = "pausedUP"
STATE_ERROR = "error"
STATE_MISSING = "missingFiles"
STATE_METADATA = "metaDL"


@dataclass
class Magnet:
    btih: str
    name: str
    size: int


def parse_magnet(link: str) -> Magnet:
    if not link.lower().startswith("magnet:?"):
        raise ValueError("not a magnet link")
    params = parse_qs(urlsplit(link).query, keep_blank_values=True)
    xt = next((v for v in params.get("xt", []) if v.lower().startswith("urn:btih:")), None)
    if xt is None:
        raise ValueError("magnet link without btih")
    btih = xt[len("urn:btih:") :].lower()
    name = unquote(params.get("dn", [""])[0])
    try:
        size = int(params.get("xl", ["0"])[0])
    except ValueError:
        size = 0
    return Magnet(btih, name, size)


class QBittorrentAPI:
    def __init__(self, ec: ECClient, state: State, search: SearchService, config: Config):
        self.ec = ec
        self.state = state
        self.search = search
        self.config = config
        self._sessions: set[str] = set()

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def handle(self, method: str, path: str, query: dict[str, str], form: dict[str, str], cookies: dict[str, str]) -> Response:
        route = path[len("/api/v2/") :].strip("/")
        params = {**query, **form}

        if route == "auth/login":
            return self._login(params)
        if route == "auth/logout":
            return Response.text("")
        if route == "app/webapiVersion":
            return Response.text(API_VERSION)
        if self.config.qbt_auth_enabled and cookies.get("SID") not in self._sessions:
            return Response.text("Forbidden", 403)

        handler = getattr(self, "_route_" + route.replace("/", "_"), None)
        if handler is None:
            log.debug("unhandled qBittorrent endpoint %s %s", method, route)
            return Response.text("Not Found", 404)
        try:
            return handler(params)
        except ECError as exc:
            log.error("aMule error while serving %s: %s", route, exc)
            return Response.text(f"aMule error: {exc}", 500)

    def _login(self, params: dict[str, str]) -> Response:
        if self.config.qbt_auth_enabled:
            if params.get("username") != self.config.qbt_username or params.get("password") != self.config.qbt_password:
                return Response.text("Fails.", 403)
        sid = secrets.token_urlsafe(24)
        self._sessions.add(sid)
        return Response(200, b"Ok.", "text/plain; charset=utf-8", [("Set-Cookie", f"SID={sid}; Path=/; HttpOnly")])

    # ------------------------------------------------------------------
    # app/*
    # ------------------------------------------------------------------

    def _route_app_version(self, params) -> Response:
        return Response.text(APP_VERSION)

    def _route_app_buildInfo(self, params) -> Response:
        return Response.json({"qt": "6.4.2", "libtorrent": "2.0.9.0", "boost": "1.81.0", "openssl": "3.0.9", "bitness": 64})

    def _route_app_preferences(self, params) -> Response:
        return Response.json(
            {
                "save_path": self._incoming_dir(),
                "temp_path_enabled": False,
                "max_ratio_enabled": False,
                "max_ratio": -1,
                "max_seeding_time_enabled": False,
                "max_seeding_time": -1,
                "max_inactive_seeding_time_enabled": False,
                "max_inactive_seeding_time": -1,
                "max_ratio_act": 0,
                "queueing_enabled": True,
                "dht": True,
                "pex": False,
                "lsd": False,
                "create_subfolder_enabled": False,
                "torrent_content_layout": "NoSubfolder",
                "start_paused_enabled": False,
                "auto_tmm_enabled": False,
            }
        )

    def _route_app_setPreferences(self, params) -> Response:
        return Response.text("")

    def _route_app_defaultSavePath(self, params) -> Response:
        return Response.text(self._incoming_dir())

    # ------------------------------------------------------------------
    # transfer / sync
    # ------------------------------------------------------------------

    def _route_transfer_info(self, params) -> Response:
        stats = self.ec.stats()
        return Response.json(
            {
                "dl_info_speed": stats["dl_speed"],
                "dl_info_data": 0,
                "up_info_speed": stats["ul_speed"],
                "up_info_data": 0,
                "dl_rate_limit": stats["dl_limit"] * 1024,
                "up_rate_limit": stats["ul_limit"] * 1024,
                "dht_nodes": stats["kad_users"],
                "connection_status": "connected",
            }
        )

    def _route_sync_maindata(self, params) -> Response:
        items = self._items()
        return Response.json(
            {
                "rid": int(time.time()),
                "full_update": True,
                "torrents": {i["hash"]: i for i in items},
                "categories": self._categories(),
                "tags": [],
                "server_state": {"connection_status": "connected", "dht_nodes": 0},
            }
        )

    # ------------------------------------------------------------------
    # torrents/*
    # ------------------------------------------------------------------

    def _route_torrents_info(self, params) -> Response:
        items = self._items()
        category = params.get("category")
        if category is not None:
            items = [i for i in items if i["category"] == category]
        hashes = self._hashes(params)
        if hashes:
            items = [i for i in items if i["hash"] in hashes]
        return Response.json(items)

    def _route_torrents_properties(self, params) -> Response:
        item = self._item(params.get("hash", ""))
        if item is None:
            return Response.text("Not Found", 404)
        return Response.json(
            {
                "save_path": item["save_path"],
                "creation_date": item["added_on"],
                "addition_date": item["added_on"],
                "completion_date": item["completion_on"],
                "total_size": item["size"],
                "total_downloaded": item["downloaded"],
                "total_uploaded": 0,
                "dl_speed": item["dlspeed"],
                "up_speed": 0,
                "eta": item["eta"],
                "seeds": item["num_seeds"],
                "seeds_total": item["num_complete"],
                "peers": 0,
                "peers_total": 0,
                "share_ratio": 0,
                "seeding_time": 0,
                "pieces_have": 0,
                "pieces_num": 0,
                "nb_connections": item["num_seeds"],
                "comment": item["magnet_uri"],
            }
        )

    def _route_torrents_files(self, params) -> Response:
        item = self._item(params.get("hash", ""))
        if item is None:
            return Response.text("Not Found", 404)
        return Response.json(
            [
                {
                    "index": 0,
                    "name": item["name"],
                    "size": item["size"],
                    "progress": item["progress"],
                    "priority": 1,
                    "is_seed": item["progress"] >= 1.0,
                    "piece_range": [0, 0],
                    "availability": 1 if item["num_complete"] else 0,
                }
            ]
        )

    def _route_torrents_categories(self, params) -> Response:
        return Response.json(self._categories())

    def _route_torrents_createCategory(self, params) -> Response:
        name = params.get("category", "").strip()
        if not name:
            return Response.text("Category name is empty", 400)
        self.state.add_category(name, params.get("savePath", ""))
        return Response.text("")

    def _route_torrents_editCategory(self, params) -> Response:
        return self._route_torrents_createCategory(params)

    def _route_torrents_removeCategories(self, params) -> Response:
        for name in params.get("categories", "").split("\n"):
            if name.strip():
                self.state.remove_category(name.strip())
        return Response.text("")

    def _route_torrents_setCategory(self, params) -> Response:
        category = params.get("category", "")
        if category and category not in self.state.categories:
            return Response.text("Incorrect category name", 409)
        for btih in self._hashes(params):
            dl = self.state.get(btih)
            if dl is not None:
                dl.category = category
                self.state.update(dl)
        return Response.text("")

    def _route_torrents_add(self, params) -> Response:
        urls = [u.strip() for u in params.get("urls", "").replace("\r", "").split("\n") if u.strip()]
        if not urls:
            return Response.text("Fails.", 415)
        category = params.get("category", "")
        paused = params.get("paused", params.get("stopped", "false")).lower() == "true"
        added = 0
        for url in urls:
            try:
                self._add_magnet(url, category, paused)
                added += 1
            except (ValueError, ECError) as exc:
                log.error("cannot add %s: %s", url, exc)
        return Response.text("Ok." if added else "Fails.", 200 if added else 415)

    def _add_magnet(self, url: str, category: str, paused: bool) -> None:
        if url.lower().startswith("ed2k://"):
            dl = self._download_from_ed2k_link(url, category)
        else:
            magnet = parse_magnet(url)
            ed2k = btih_to_ed2k(magnet.btih)
            name, size = magnet.name, magnet.size
            if not name or not size:
                known = self.search.find_known(ed2k)
                if known is None:
                    raise ValueError(f"unknown file {ed2k}: magnet lacks dn/xl and it is not in the search cache")
                name, size = known.name, known.size
            dl = Download(magnet.btih, ed2k, name, size, category)
        if category and category not in self.state.categories:
            self.state.add_category(category)
        try:
            self.ec.add_link(dl.ed2k_link)
        except ECError as exc:
            if "already" not in str(exc).lower():
                raise
            log.info("%s already in aMule queue", dl.name)
        dl.paused = paused
        self.state.add(dl)
        if paused:
            try:
                self.ec.pause(dl.ed2k)
            except ECError as exc:
                log.warning("cannot pause %s: %s", dl.name, exc)
        log.info("added %s (%d bytes, category %r) to aMule", dl.name, dl.size, category)

    @staticmethod
    def _download_from_ed2k_link(link: str, category: str) -> Download:
        parts = link.split("|")
        if len(parts) < 5 or parts[1] != "file":
            raise ValueError("malformed ed2k link")
        name, size, ed2k = unquote(parts[2]), int(parts[3]), parts[4].upper()
        from .state import ed2k_to_btih

        return Download(ed2k_to_btih(ed2k), ed2k, name, size, category)

    def _route_torrents_delete(self, params) -> Response:
        delete_files = params.get("deleteFiles", "false").lower() == "true"
        queue = self._queue()
        for btih in self._hashes(params):
            dl = self.state.get(btih)
            if dl is None:
                continue
            qf = queue.get(dl.ed2k)
            if qf is not None and not qf.is_complete:
                try:
                    self.ec.delete(dl.ed2k)
                except ECError as exc:
                    log.warning("cannot cancel %s in aMule: %s", dl.name, exc)
            elif qf is not None and qf.is_complete and self.config.clear_completed_in_amule:
                try:
                    self.ec.clear_completed([qf.ecid])
                except ECError as exc:
                    log.debug("cannot clear completed %s: %s", dl.name, exc)
            if delete_files:
                self._delete_local_file(dl)
            self.state.remove(btih)
            log.info("removed %s (deleteFiles=%s)", dl.name, delete_files)
        return Response.text("")

    def _delete_local_file(self, dl: Download) -> None:
        local_dir = self._local_incoming_dir()
        if not local_dir:
            return
        path = os.path.join(local_dir, dl.name)
        if os.path.isfile(path):
            try:
                os.remove(path)
                log.info("deleted %s", path)
            except OSError as exc:
                log.warning("cannot delete %s: %s", path, exc)

    def _route_torrents_pause(self, params) -> Response:
        return self._pause_resume(params, True)

    _route_torrents_stop = _route_torrents_pause

    def _route_torrents_resume(self, params) -> Response:
        return self._pause_resume(params, False)

    _route_torrents_start = _route_torrents_resume

    def _pause_resume(self, params, pause: bool) -> Response:
        for btih in self._hashes(params):
            dl = self.state.get(btih)
            if dl is None:
                continue
            try:
                if pause:
                    self.ec.pause(dl.ed2k)
                else:
                    self.ec.resume(dl.ed2k)
            except ECError as exc:
                log.warning("cannot %s %s: %s", "pause" if pause else "resume", dl.name, exc)
            dl.paused = pause
            self.state.update(dl)
        return Response.text("")

    def _route_torrents_setForceStart(self, params) -> Response:
        if params.get("value", "false").lower() == "true":
            return self._pause_resume(params, False)
        return Response.text("")

    # Accepted but meaningless for ed2k.
    def _noop(self, params) -> Response:
        return Response.text("")

    _route_torrents_topPrio = _noop
    _route_torrents_bottomPrio = _noop
    _route_torrents_increasePrio = _noop
    _route_torrents_decreasePrio = _noop
    _route_torrents_setShareLimits = _noop
    _route_torrents_setLocation = _noop
    _route_torrents_rename = _noop
    _route_torrents_recheck = _noop
    _route_torrents_reannounce = _noop
    _route_torrents_addTags = _noop
    _route_torrents_removeTags = _noop
    _route_torrents_setAutoManagement = _noop
    _route_torrents_toggleSequentialDownload = _noop
    _route_torrents_toggleFirstLastPiecePrio = _noop
    _route_torrents_setSuperSeeding = _noop
    _route_torrents_setDownloadLimit = _noop
    _route_torrents_setUploadLimit = _noop

    # ------------------------------------------------------------------
    # Item construction
    # ------------------------------------------------------------------

    def _incoming_dir(self) -> str:
        path = self.config.incoming_dir
        if not path:
            path = self.ec.directories()[0] or "/"
        return path.rstrip("/") + "/"

    def _local_incoming_dir(self) -> str | None:
        return self.config.local_incoming_dir or self.config.incoming_dir

    def _categories(self) -> dict:
        return {name: {"name": name, "savePath": path} for name, path in self.state.categories.items()}

    @staticmethod
    def _hashes(params) -> set[str]:
        raw = params.get("hashes", params.get("hash", ""))
        if raw == "all":
            return set()
        return {h.strip().lower() for h in raw.split("|") if h.strip()}

    def _queue(self) -> dict[str, QueuedFile]:
        try:
            return {f.hash: f for f in self.ec.download_queue()}
        except ECError as exc:
            log.error("cannot read aMule queue: %s", exc)
            return {}

    def _item(self, btih: str) -> dict | None:
        for item in self._items():
            if item["hash"] == btih.lower():
                return item
        return None

    def _items(self) -> list[dict]:
        queue = self._queue()
        incoming = self._incoming_dir()
        local_dir = self._local_incoming_dir()
        now = int(time.time())
        items = []
        for dl in self.state.all():
            qf = queue.get(dl.ed2k)
            done = dl.size
            speed = 0
            sources = 0
            sources_xfer = 0
            if qf is not None:
                done = qf.done
                speed = qf.speed
                sources = qf.sources
                sources_xfer = qf.sources_xfer
                state = self._state_from_queue(qf, dl)
                if qf.name and qf.name != dl.name:
                    dl.name = qf.name
                    self.state.update(dl)
            elif dl.completed_on:
                state = STATE_COMPLETED
            elif local_dir and os.path.isfile(os.path.join(local_dir, dl.name)):
                state = STATE_COMPLETED
            elif now - dl.added_on < 120:
                state = STATE_METADATA
            else:
                state = STATE_MISSING
                done = 0
            if state == STATE_COMPLETED:
                done = dl.size
                if not dl.completed_on:
                    dl.completed_on = now
                    self.state.update(dl)
            progress = (done / dl.size) if dl.size else 0.0
            remaining = max(dl.size - done, 0)
            if state == STATE_COMPLETED:
                eta = 0
            elif speed > 0:
                eta = min(int(remaining / speed), ETA_INFINITE)
            else:
                eta = ETA_INFINITE
            items.append(
                {
                    "hash": dl.hash,
                    "name": dl.name,
                    "size": dl.size,
                    "total_size": dl.size,
                    "progress": round(progress, 4),
                    "dlspeed": speed,
                    "upspeed": 0,
                    "downloaded": done,
                    "uploaded": 0,
                    "amount_left": remaining,
                    "completed": done,
                    "eta": eta,
                    "state": state,
                    "category": dl.category,
                    "tags": "",
                    "save_path": incoming,
                    "content_path": incoming + dl.name,
                    "download_path": "",
                    "added_on": dl.added_on,
                    "completion_on": dl.completed_on or -1,
                    "last_activity": now if speed else dl.completed_on or dl.added_on,
                    "seen_complete": now if sources else 0,
                    "num_seeds": sources_xfer,
                    "num_complete": sources,
                    "num_leechs": 0,
                    "num_incomplete": 0,
                    "ratio": 0.0,
                    "ratio_limit": 0.0,
                    "seeding_time": 0,
                    "seeding_time_limit": -2,
                    "inactive_seeding_time_limit": -2,
                    "priority": 0,
                    "seq_dl": False,
                    "f_l_piece_prio": False,
                    "auto_tmm": False,
                    "super_seeding": False,
                    "force_start": False,
                    "tracker": "",
                    "trackers_count": 0,
                    "magnet_uri": f"magnet:?xt=urn:btih:{dl.hash}&dn={dl.name}&xl={dl.size}",
                    "availability": 1.0 if sources else 0.0,
                    "dl_limit": -1,
                    "up_limit": -1,
                    "max_ratio": -1,
                    "max_seeding_time": -1,
                    "max_inactive_seeding_time": -1,
                    "time_active": max(now - dl.added_on, 0),
                    "infohash_v1": dl.hash,
                    "infohash_v2": "",
                }
            )
        return items

    @staticmethod
    def _state_from_queue(qf: QueuedFile, dl: Download) -> str:
        status = qf.status
        if status == C.PS_COMPLETE:
            return STATE_COMPLETED
        if status == C.PS_PAUSED or qf.stopped or dl.paused:
            return STATE_PAUSED_DL
        if status in (C.PS_ERROR, C.PS_INSUFFICIENT):
            return STATE_ERROR
        if status in (C.PS_COMPLETING, C.PS_HASHING, C.PS_WAITING_FOR_HASH, C.PS_ALLOCATING):
            return STATE_CHECKING_DL
        if qf.sources_xfer > 0 or qf.speed > 0:
            return STATE_DOWNLOADING
        return STATE_STALLED
