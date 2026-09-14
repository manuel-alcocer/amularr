# Installing with Docker

*English · [Español](es/instalacion-docker.md)*

amularr is a single stateless-ish container (one small JSON state file). It
needs network access to an aMule daemon and a view of aMule's Incoming
directory. Images for `linux/amd64` and `linux/arm64` are published at
`ghcr.io/manuel-alcocer/amularr`.

## 1. Prepare aMule

amularr talks to aMule through External Connections (EC), the same protocol
`amulecmd` and `amulegui` use.

1. Enable EC in `amule.conf` (or in the aMule preferences, *Remote Controls*):

   ```ini
   [ExternalConnect]
   AcceptExternalConnections=1
   ECPort=4712
   ECPassword=<md5 of your password>
   ```

   `ECPassword` is the MD5 hex of the password:
   `echo -n 'my-password' | md5sum`. Restart aMule afterwards.
2. If aMule runs in Docker (for example `ngosang/amule`), publish port 4712
   and note the path of its Incoming directory on the host.
3. Test from the machine that will run amularr:

   ```bash
   amulecmd -h <amule-host> -p 4712 -P 'my-password' -c status
   ```

## 2. Decide the paths

Sonarr and Radarr import finished files from aMule's Incoming directory, so
the same directory must be visible to three parties:

| Who | Mount | Variable |
| --- | --- | --- |
| aMule | its Incoming directory | |
| amularr | for example `/amule/incoming` | `AMULARR_LOCAL_INCOMING_DIR` |
| Sonarr / Radarr | for example `/amule/incoming` | `AMULARR_INCOMING_DIR` (what amularr reports to them) |

Use the same path in Sonarr/Radarr and in `AMULARR_INCOMING_DIR`, or add a
*Remote Path Mapping* in Sonarr/Radarr for the amularr download client.

## 3. Run it

### docker run

```bash
docker run -d --name amularr --restart unless-stopped \
  --user 1000:100 \
  -p 8080:8080 \
  -e AMULE_EC_HOST=192.168.1.12 \
  -e AMULE_EC_PASSWORD='my-password' \
  -e AMULARR_INCOMING_DIR=/amule/incoming \
  -e AMULARR_LOCAL_INCOMING_DIR=/amule/incoming \
  -v amularr-data:/data \
  -v /srv/amule/incoming:/amule/incoming \
  ghcr.io/manuel-alcocer/amularr:0.2.0
```

Run it with the same uid/gid as your Sonarr/Radarr containers so amularr can
delete an imported file when Sonarr asks for it.

### docker compose

`deploy/docker-compose.yml` contains the same setup with every option
commented, plus an optional aMule service (`--profile with-amule`) for a
stack that has no aMule yet:

```bash
cp deploy/docker-compose.yml ./docker-compose.yml
# edit the paths, the EC password and, if you want, the *arr API keys
docker compose up -d
docker compose logs -f amularr
```

Prefer `AMULE_EC_PASSWORD_MD5` (the `Password=` line of `~/.aMule/remote.conf`)
over the clear-text password when the compose file is committed somewhere.

## 4. Check

```bash
curl -s http://localhost:8080/health | jq
curl -s 'http://localhost:8080/api?t=caps' | head
```

`/health` returns `"ok": true` and the aMule version once the EC link is up.
`/ping` answers `pong` without touching aMule (use it for container health
checks). Then wire Prowlarr, Sonarr and Radarr as described in
[usage.md](usage.md).

## Updating

```bash
docker compose pull amularr && docker compose up -d amularr
```

The state file in the `amularr-data` volume is forward compatible; keep the
volume across upgrades or in-flight downloads will lose their fake torrent
hashes and Sonarr/Radarr will report them as removed.
