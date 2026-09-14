"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .ec import codes as C

DEFAULT_VIDEO_EXTENSIONS = (
    "mkv avi mp4 m4v mov wmv mpg mpeg ts m2ts vob divx xvid ogm webm flv rmvb iso img"
).split()

SEARCH_TYPES = {
    "global": C.SEARCH_GLOBAL,
    "local": C.SEARCH_LOCAL,
    "kad": C.SEARCH_KAD,
}


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _env_int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    # aMule External Connections
    ec_host: str = "127.0.0.1"
    ec_port: int = 4712
    ec_password: str | None = None
    ec_password_md5: str | None = None
    ec_timeout: float = 30.0

    # HTTP server
    listen_host: str = "0.0.0.0"
    listen_port: int = 8080

    # Torznab
    api_key: str | None = None
    search_type: int = C.SEARCH_GLOBAL
    search_timeout: float = 25.0
    search_poll_interval: float = 1.0
    search_cache_ttl: float = 600.0
    min_sources: int = 1
    max_results: int = 200
    video_extensions: list[str] = field(default_factory=lambda: list(DEFAULT_VIDEO_EXTENSIONS))
    file_type: str = "Video"  # ed2k type filter for tv/movie searches ("" disables)
    indexer_name: str = "amularr"
    # Keyword search used to answer RSS-style requests (no q) when nothing
    # has been searched recently; Prowlarr rejects an indexer whose test
    # feed is empty.
    rss_query: str = "1080p"

    # Wanted-list searches (see wanted.py): ask Sonarr/Radarr what they are
    # missing and search aMule for it in the background, so the RSS feed can
    # surface new episodes/movies without a manual search.
    sonarr_url: str | None = None
    sonarr_api_key: str | None = None
    radarr_url: str | None = None
    radarr_api_key: str | None = None
    wanted_days: int = 7  # only items aired/released this many days ago at most
    wanted_interval: float = 900.0  # seconds between two reads of the wanted lists
    wanted_research_interval: float = 21600.0  # seconds before a wanted item is searched again
    wanted_max_searches: int = 30  # keyword searches per refresh
    wanted_max_titles: int = 3  # titles (main + alternate) tried per item
    wanted_page_size: int = 200
    wanted_title_languages: list[str] = field(default_factory=lambda: ["spanish"])

    # qBittorrent emulation
    qbt_username: str | None = None
    qbt_password: str | None = None
    # Incoming directory as seen by Sonarr/Radarr (reported in content_path).
    incoming_dir: str | None = None
    # Incoming directory as seen by amularr itself (used to detect finished files).
    local_incoming_dir: str | None = None
    state_file: str = "/data/amularr-state.json"
    # Report finished downloads as pausedUP so the *arr apps remove them
    # after import; keep completed entries in aMule's list otherwise.
    clear_completed_in_amule: bool = True

    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        search_type = _env("AMULARR_SEARCH_TYPE", "global").lower()
        if search_type not in SEARCH_TYPES:
            raise ValueError(f"AMULARR_SEARCH_TYPE must be one of {', '.join(SEARCH_TYPES)}")
        extensions = _env("AMULARR_VIDEO_EXTENSIONS")
        languages = _env("AMULARR_WANTED_TITLE_LANGUAGES")
        cfg = cls(
            ec_host=_env("AMULE_EC_HOST", "127.0.0.1"),
            ec_port=_env_int("AMULE_EC_PORT", 4712),
            ec_password=_env("AMULE_EC_PASSWORD"),
            ec_password_md5=_env("AMULE_EC_PASSWORD_MD5"),
            ec_timeout=_env_float("AMULE_EC_TIMEOUT", 30.0),
            listen_host=_env("AMULARR_LISTEN_HOST", "0.0.0.0"),
            listen_port=_env_int("AMULARR_LISTEN_PORT", 8080),
            api_key=_env("AMULARR_API_KEY"),
            search_type=SEARCH_TYPES[search_type],
            search_timeout=_env_float("AMULARR_SEARCH_TIMEOUT", 25.0),
            search_poll_interval=_env_float("AMULARR_SEARCH_POLL_INTERVAL", 1.0),
            search_cache_ttl=_env_float("AMULARR_SEARCH_CACHE_TTL", 600.0),
            min_sources=_env_int("AMULARR_MIN_SOURCES", 1),
            max_results=_env_int("AMULARR_MAX_RESULTS", 200),
            video_extensions=extensions.replace(",", " ").split() if extensions else list(DEFAULT_VIDEO_EXTENSIONS),
            file_type=_env("AMULARR_FILE_TYPE", "Video"),
            indexer_name=_env("AMULARR_INDEXER_NAME", "amularr"),
            rss_query=_env("AMULARR_RSS_QUERY", "1080p"),
            sonarr_url=_env("AMULARR_SONARR_URL"),
            sonarr_api_key=_env("AMULARR_SONARR_API_KEY"),
            radarr_url=_env("AMULARR_RADARR_URL"),
            radarr_api_key=_env("AMULARR_RADARR_API_KEY"),
            wanted_days=_env_int("AMULARR_WANTED_DAYS", 7),
            wanted_interval=_env_float("AMULARR_WANTED_INTERVAL", 900.0),
            wanted_research_interval=_env_float("AMULARR_WANTED_RESEARCH_INTERVAL", 21600.0),
            wanted_max_searches=_env_int("AMULARR_WANTED_MAX_SEARCHES", 30),
            wanted_max_titles=_env_int("AMULARR_WANTED_MAX_TITLES", 3),
            wanted_page_size=_env_int("AMULARR_WANTED_PAGE_SIZE", 200),
            wanted_title_languages=languages.replace(",", " ").split() if languages else ["spanish"],
            qbt_username=_env("AMULARR_QBT_USERNAME"),
            qbt_password=_env("AMULARR_QBT_PASSWORD"),
            incoming_dir=_env("AMULARR_INCOMING_DIR"),
            local_incoming_dir=_env("AMULARR_LOCAL_INCOMING_DIR"),
            state_file=_env("AMULARR_STATE_FILE", "/data/amularr-state.json"),
            clear_completed_in_amule=_env_bool("AMULARR_CLEAR_COMPLETED", True),
            log_level=_env("AMULARR_LOG_LEVEL", "INFO").upper(),
        )
        if cfg.file_type.lower() in ("", "any", "none"):
            cfg.file_type = ""
        if not cfg.ec_password and not cfg.ec_password_md5:
            raise ValueError("AMULE_EC_PASSWORD or AMULE_EC_PASSWORD_MD5 is required")
        return cfg

    @property
    def qbt_auth_enabled(self) -> bool:
        return bool(self.qbt_username and self.qbt_password)
