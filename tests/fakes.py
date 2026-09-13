"""In-memory stand-in for ECClient used by the API tests."""

from __future__ import annotations

from urllib.parse import unquote

from amularr.ec import ECError, QueuedFile, SearchResult
from amularr.ec import codes as C


class FakeEC:
    def __init__(self):
        self.server_version = "fake"
        self.results: list[SearchResult] = []
        self.queue: dict[str, QueuedFile] = {}
        self.searches: list[tuple[str, int, str]] = []
        self.added_links: list[str] = []
        self.deleted: list[str] = []
        self.paused: list[str] = []
        self.resumed: list[str] = []
        self.cleared: list[list[int]] = []
        self.progress_values = [100]
        self.incoming = "/data/incoming"
        self.fail_add = False

    # connection -----------------------------------------------------
    def connect(self):
        pass

    def close(self):
        pass

    def conn_state(self):
        from amularr.ec.client import ConnState

        return ConnState(True, False, True, False, True, 1, 1, "Fake Server")

    def stats(self):
        return {"ul_speed": 1, "dl_speed": 2, "ul_limit": 0, "dl_limit": 0, "sources": 0, "ul_queue": 0, "ed2k_users": 3, "kad_users": 4}

    def directories(self):
        return self.incoming, "/data/temp"

    # search ---------------------------------------------------------
    def search_start(self, text, search_type=C.SEARCH_GLOBAL, file_type="", extension="", min_size=0, max_size=0, availability=0):
        self.searches.append((text, search_type, file_type))
        return "Search in progress. Refetch results in a moment!"

    def search_progress(self):
        return self.progress_values[min(len(self.searches), len(self.progress_values)) - 1]

    def search_results(self):
        return list(self.results)

    def search_stop(self):
        pass

    # downloads ------------------------------------------------------
    def add_link(self, link, category=0):
        if self.fail_add:
            raise ECError("Invalid link or already on list.")
        self.added_links.append(link)
        parts = link.split("|")
        ed2k = parts[4].upper()
        if ed2k not in self.queue:
            self.queue[ed2k] = make_queued(ed2k, unquote(parts[2]), int(parts[3]))

    def download_queue(self):
        return list(self.queue.values())

    def delete(self, file_hash):
        self.deleted.append(file_hash)
        self.queue.pop(file_hash.upper(), None)

    def pause(self, file_hash):
        self.paused.append(file_hash)

    def resume(self, file_hash):
        self.resumed.append(file_hash)

    def stop(self, file_hash):
        pass

    def clear_completed(self, ecids):
        self.cleared.append(list(ecids))


def make_result(ed2k: str, name: str, size: int, sources: int = 5) -> SearchResult:
    return SearchResult(ecid=1, hash=ed2k.upper(), name=name, size=size, sources=sources, complete_sources=sources, status=0)


def make_queued(ed2k: str, name: str, size: int, done: int = 0, status: int = C.PS_READY, speed: int = 0, sources_xfer: int = 0) -> QueuedFile:
    return QueuedFile(
        ecid=7,
        hash=ed2k.upper(),
        name=name,
        size=size,
        done=done,
        transferred=done,
        speed=speed,
        status=status,
        stopped=False,
        sources=3,
        sources_xfer=sources_xfer,
        category=0,
        part_met="001.part.met",
        last_seen_complete=0,
        ed2k_link=f"ed2k://|file|{name}|{size}|{ed2k.upper()}|/",
    )
