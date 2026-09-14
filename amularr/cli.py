"""Entry point: ``amularr`` starts the bridge server."""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__
from .config import Config
from .ec import ECClient, ECError
from .httpd import Bridge, Server
from .qbittorrent import QBittorrentAPI
from .search import SearchService
from .state import State
from .torznab import TorznabAPI
from .wanted import WantedSearcher


def build_bridge(config: Config) -> Bridge:
    ec = ECClient(
        config.ec_host,
        config.ec_port,
        password=config.ec_password,
        password_md5=config.ec_password_md5,
        client_version=__version__,
        timeout=config.ec_timeout,
    )
    state = State(config.state_file)
    search = SearchService(ec, config)
    wanted = WantedSearcher(search, config)
    torznab = TorznabAPI(search, config, wanted)
    qbittorrent = QBittorrentAPI(ec, state, search, config)
    return Bridge(config, ec, torznab, qbittorrent)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="amularr", description="Torznab + qBittorrent bridge for aMule")
    parser.add_argument("--version", action="version", version=f"amularr {__version__}")
    parser.parse_args(argv)

    try:
        config = Config.from_env()
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("amularr")
    bridge = build_bridge(config)
    try:
        bridge.ec.connect()
        incoming, temp = bridge.ec.directories()
        log.info("aMule incoming dir %s, temp dir %s", incoming, temp)
        if not config.incoming_dir:
            log.warning("AMULARR_INCOMING_DIR not set, reporting aMule's own path %s to the *arr apps", incoming)
    except ECError as exc:
        log.error("cannot connect to aMule yet (%s); will retry on first request", exc)

    server = Server(bridge)
    log.info("amularr %s listening on %s:%d", __version__, config.listen_host, config.listen_port)
    if bridge.torznab.wanted.sources():
        log.info("wanted-list searches enabled for: %s", ", ".join(bridge.torznab.wanted.sources()))
    else:
        log.info("wanted-list searches disabled (set AMULARR_SONARR_URL/API_KEY or AMULARR_RADARR_URL/API_KEY)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        bridge.ec.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
