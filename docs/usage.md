# Usage: wiring Prowlarr, Sonarr and Radarr

*English · [Español](es/uso.md)*

amularr exposes two facades on one port:

| Path | Speaks | Registered in |
| --- | --- | --- |
| `/api` | Torznab | Prowlarr, as an indexer |
| `/api/v2/...` | qBittorrent Web API v2 | Sonarr and Radarr, as a download client |

Every ed2k result becomes a fake torrent whose info hash embeds the ed2k
hash, so a release "grabbed" from the indexer can be handed back to the
download-client facade, which turns it into an `ed2k://` link for aMule.
Both halves must therefore be registered, and grabs from the amularr indexer
must go to the amularr download client (see *Routing* below).

## 1. Prowlarr: the indexer

*Indexers → Add Indexer → Generic Torznab*:

| Field | Value |
| --- | --- |
| Name | `aMule (amularr)` |
| URL | `http://amularr:8080` (or `http://amularr.<namespace>.svc:8080`) |
| API Path | `/api` |
| API Key | the value of `AMULARR_API_KEY`, or empty |
| Categories | Movies and TV (the caps advertise 2000/2030/2040/2045 and 5000/5030/5040/5045) |
| Sync | *Full Sync* to Sonarr and Radarr |

*Test* runs a search without keywords; amularr answers it with the results
of recent searches or with the `AMULARR_RSS_QUERY` keyword search (`1080p`
by default), so the test never fails on an empty feed.

Searches map like this:

| Prowlarr request | ed2k keyword searches |
| --- | --- |
| `t=tvsearch&q=Dark&season=3&ep=3` | `Dark S03E03`, `Dark 3x03` |
| `t=tvsearch&q=Dark&season=3` | `Dark S03`, `Dark temporada 3` |
| `t=movie&q=Blade Runner&year=1982` | `Blade Runner 1982`, `Blade Runner` |
| `t=search&q=...` | the query as is |

Sonarr and Radarr send every title they know (original, scene and alternate
titles), so Spanish releases named after the local title are found too.
Results are filtered to video extensions for TV/movie categories, sorted by
number of sources (reported as seeders), and classified into SD/HD/UHD by
their name.

## 2. Sonarr and Radarr: the download client

*Settings → Download Clients → Add → qBittorrent*:

| Field | Value |
| --- | --- |
| Name | `aMule (amularr)` |
| Host / Port | `amularr` / `8080` (no SSL, no URL base) |
| Username / Password | only if `AMULARR_QBT_USERNAME` / `AMULARR_QBT_PASSWORD` are set |
| Category | `tv-sonarr` / `radarr` (any name; amularr keeps categories in its state file) |
| Priority | lower than your real torrent client if you have one (see *Routing*) |
| Remove Completed | on: Sonarr/Radarr delete the entry (and the file, if asked) after importing |

The client reports:

- `downloading`, `stalledDL` (no sources), `pausedDL`, `queuedDL` while aMule
  works on the file;
- `pausedUP` with `progress = 1` once the file is complete and sits in
  Incoming, so the *arr app imports it and then removes it;
- `error` when aMule refused or dropped the link.

`content_path` is `AMULARR_INCOMING_DIR/<file name>`; that directory must be
reachable by Sonarr/Radarr at that same path (or through a remote path
mapping for this client).

## 3. Routing: keep amularr grabs away from a real qBittorrent

Sonarr and Radarr choose a download client by *protocol and priority*, not
by indexer. If a real qBittorrent is registered with a better priority,
every magnet from the amularr indexer goes there and stalls in `metaDL`
(those fake hashes end in `00000000`).

The fix is on the **indexer**: in Sonarr/Radarr *Settings → Indexers*, open
`aMule (amularr) (Prowlarr)` and set *Download Client* to `aMule (amularr)`.
Prowlarr keeps that field across syncs. Do not use download-client tags for
this: they match series/movie tags, not indexers.

With the API, that is `downloadClientId` on `/api/v3/indexer/<id>`.

## 4. Wanted-list searches (automatic grabs)

ed2k/Kad has no "recent releases" feed, so the RSS answer alone can never
surface a freshly aired episode, and Sonarr/Radarr only grab automatically
what shows up in their RSS sync. To make automatic grabs work, give amularr
read access to the *arr apps:

```
AMULARR_SONARR_URL=http://sonarr:8989   AMULARR_SONARR_API_KEY=<Settings → General → API Key>
AMULARR_RADARR_URL=http://radarr:7878   AMULARR_RADARR_API_KEY=<Settings → General → API Key>
```

With those set, every RSS request from Sonarr/Radarr makes amularr, in the
background:

1. read *Wanted → Missing* (monitored items aired, released or added within
   `AMULARR_WANTED_DAYS`, default 7);
2. run the same keyword searches the *arr app would (`Show S01E05` and
   `Show 1x05` for every known title; `Movie 2022` for the title, the
   original title and the alternate titles in `AMULARR_WANTED_TITLE_LANGUAGES`);
3. keep the hits in the feed until the item is no longer wanted.

The next RSS sync (every 15 minutes by default in Sonarr/Radarr) then sees
the release and grabs it if it passes the quality profile. A wanted item is
searched again every `AMULARR_WANTED_RESEARCH_INTERVAL` seconds (6 hours),
so a release that appears on the network a day later is still picked up.
Each refresh runs at most `AMULARR_WANTED_MAX_SEARCHES` keyword searches (30),
and each search takes up to `AMULARR_SEARCH_TIMEOUT` seconds, so a large
backlog is worked through over several syncs.

The log shows what happened:

```
wanted tv: Lanterns S01E05 -> 6 results (Lanterns S01E05, Lanterns 1x05, Linternas S01E05, Linternas 1x05)
wanted tv: 1 items wanted, 1 searched now (6 hits), 0 postponed
```

## 5. Watching it work

- `GET /health` → aMule version, ed2k/Kad connection state, `ok`.
- Logs: every Torznab request with its query, categories and result count;
  every EC search with duration and result count; every add/delete sent to
  aMule.
- Sonarr/Radarr *Activity → Queue* shows the aMule downloads with aMule's
  progress and source count.

## Troubleshooting

**Prowlarr test fails / empty feed.** Check `/health`; if aMule is not
connected to ed2k or Kad every search returns nothing. Set `AMULARR_RSS_QUERY`
to something common in your language if the default `1080p` finds nothing.

**Grabs land in the real qBittorrent.** See *Routing*. Delete the fake
entries there (hash suffix `00000000`) after fixing the indexer's download
client.

**Sonarr imports nothing after aMule finishes.** `content_path` is not
reachable from Sonarr: mount Incoming at the same path or add a remote path
mapping. The Sonarr queue shows the exact path it tried.

**The download disappears from the queue after an aMule restart.** aMule
forgets completed files on restart. amularr keeps its own state file and
checks Incoming on disk, but that file must survive restarts
(`AMULARR_STATE_FILE` on a volume).

**Sonarr warns "rss sync didn't cover the period".** Items carry the time
they were first seen as `pubDate` since 0.2.0, which stops the warning;
older versions dated every item "now".
