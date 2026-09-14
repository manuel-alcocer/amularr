# amularr

*[English](README.en.md) · Español*

Puente entre el stack *arr (Prowlarr, Sonarr, Radarr) y aMule.

Habla el protocolo External Connections (EC) de aMule de forma nativa (sin
parsear la salida de `amulecmd`) y expone dos fachadas HTTP:

- **Indexer Torznab** (`/api`): cada resultado de búsqueda ed2k/Kad se
  convierte en un torrent falso cuyo info hash lleva dentro el hash ed2k,
  entregado como enlace magnet. Se da de alta en Prowlarr como indexer
  *Generic Torznab*.
- **API web de qBittorrent v2** (`/api/v2/...`): Sonarr y Radarr lo usan
  como un cliente qBittorrent normal. Los magnets que vienen del indexer se
  convierten de vuelta en enlaces `ed2k://` y se encolan en aMule; el
  progreso, la finalización y el borrado se leen de la cola de descargas de
  aMule.

Sin dependencias Python de terceros.

## Cómo funciona

```
Prowlarr ──Torznab──▶ amularr ──EC──▶ amuled  (búsqueda)
Sonarr   ──API qBittorrent──▶ amularr ──EC──▶ amuled  (añadir enlace ed2k, estado, cancelar)
Sonarr   ◀── importa el fichero terminado desde el directorio Incoming de aMule (montaje compartido)
```

1. Prowlarr busca. amularr lanza las búsquedas ed2k (para un capítulo tanto
   `Serie S03E03` como `Serie 3x03`), espera a que el núcleo termine y
   devuelve los resultados como ítems Torznab con `seeders = fuentes`.
2. Sonarr elige una release y manda su magnet al cliente "qBittorrent".
   amularr saca del magnet el hash ed2k, el nombre y el tamaño y llama a
   `OP_ADD_LINK` en aMule.
3. Sonarr consulta `torrents/info`. amularr traduce el estado del fichero
   .part de aMule a estados de qBittorrent (`downloading`, `stalledDL`,
   `pausedDL`, `pausedUP` al completarse, `error`).
4. Al completarse, `content_path` apunta al fichero dentro del directorio
   Incoming de aMule. Sonarr lo importa y llama a `torrents/delete`; amularr
   olvida la entrada y, si se lo piden, borra el fichero.

aMule olvida las descargas terminadas al reiniciarse, así que amularr guarda
su propio fichero de estado (`AMULARR_STATE_FILE`) y además comprueba el
directorio Incoming en disco.

## Configuración

Variables de entorno:

| Variable | Por defecto | Descripción |
| --- | --- | --- |
| `AMULE_EC_HOST` | `127.0.0.1` | Host de aMule (EC debe estar habilitado en amule.conf) |
| `AMULE_EC_PORT` | `4712` | Puerto EC |
| `AMULE_EC_PASSWORD` | | Contraseña EC en claro (o usa el MD5 de abajo) |
| `AMULE_EC_PASSWORD_MD5` | | Contraseña EC como MD5 hex, el mismo valor que `Password=` en `remote.conf` |
| `AMULARR_LISTEN_HOST` / `AMULARR_LISTEN_PORT` | `0.0.0.0` / `8080` | Dirección de escucha HTTP |
| `AMULARR_API_KEY` | | API key Torznab opcional |
| `AMULARR_SEARCH_TYPE` | `global` | `global`, `local` o `kad` |
| `AMULARR_SEARCH_TIMEOUT` | `25` | Segundos de espera a que termine una búsqueda |
| `AMULARR_SEARCH_CACHE_TTL` | `600` | Segundos durante los que se reutiliza un resultado de búsqueda |
| `AMULARR_MIN_SOURCES` | `1` | Descarta resultados con menos fuentes |
| `AMULARR_MAX_RESULTS` | `200` | Tope de tamaño de página Torznab |
| `AMULARR_RSS_QUERY` | `1080p` | Búsqueda por palabra clave que responde a peticiones RSS (sin `q`) cuando no hay búsquedas recientes |
| `AMULARR_FILE_TYPE` | `Video` | Filtro de tipo ed2k para búsquedas de series/películas (`any` lo desactiva) |
| `AMULARR_VIDEO_EXTENSIONS` | mkv avi mp4 ... | Extensiones aceptadas en las categorías de series/películas |
| `AMULARR_INCOMING_DIR` | la ruta de aMule | Directorio Incoming tal como lo ven Sonarr/Radarr (`content_path`) |
| `AMULARR_LOCAL_INCOMING_DIR` | igual que el anterior | Directorio Incoming tal como lo ve amularr (comprobación de finalización, borrados) |
| `AMULARR_STATE_FILE` | `/data/amularr-state.json` | Estado persistente |
| `AMULARR_QBT_USERNAME` / `AMULARR_QBT_PASSWORD` | | Credenciales opcionales de la fachada qBittorrent |
| `AMULARR_LOG_LEVEL` | `INFO` | Nivel de log |
| `AMULARR_SONARR_URL` / `AMULARR_SONARR_API_KEY` | | Activan las búsquedas de pendientes para Sonarr (ver [docs/uso.md](docs/uso.md)) |
| `AMULARR_RADARR_URL` / `AMULARR_RADARR_API_KEY` | | Activan las búsquedas de pendientes para Radarr |
| `AMULARR_WANTED_DAYS` | `7` | Solo busca elementos emitidos, estrenados o añadidos en estos últimos días |
| `AMULARR_WANTED_INTERVAL` | `900` | Segundos entre dos lecturas de las listas de pendientes |
| `AMULARR_WANTED_RESEARCH_INTERVAL` | `21600` | Segundos hasta volver a buscar un elemento que sigue pendiente |
| `AMULARR_WANTED_MAX_SEARCHES` | `30` | Búsquedas por ciclo (cada una tarda como mucho `AMULARR_SEARCH_TIMEOUT`) |
| `AMULARR_WANTED_MAX_TITLES` | `3` | Títulos probados por elemento (principal más alternativos) |
| `AMULARR_WANTED_TITLE_LANGUAGES` | `spanish` | Idiomas de los títulos alternativos de Radarr con los que se busca |

En [docs/uso.md](docs/uso.md) se explica cómo las búsquedas de
pendientes hacen posibles las capturas automáticas.

## Instalación

Hay imágenes para `linux/amd64` y `linux/arm64` en
`ghcr.io/manuel-alcocer/amularr` (`0.2.0`, `0.2`, `latest`, y `main` para la
rama de desarrollo).

- **Docker / docker compose**: [docs/instalacion-docker.md](docs/instalacion-docker.md),
  stack de ejemplo en [deploy/docker-compose.yml](deploy/docker-compose.yml).
- **Kubernetes**: [docs/instalacion-kubernetes.md](docs/instalacion-kubernetes.md),
  manifiesto en [deploy/kubernetes/amularr.yaml](deploy/kubernetes/amularr.yaml).
- **Alta en Prowlarr, Sonarr y Radarr**, enrutado, búsquedas de pendientes y
  resolución de problemas: [docs/uso.md](docs/uso.md).

Arranque rápido con un aMule que ya corre en `192.168.1.12`:

```bash
docker run -d --name amularr -p 8080:8080 --user 1000:100 \
  -e AMULE_EC_HOST=192.168.1.12 -e AMULE_EC_PASSWORD=secreto \
  -e AMULARR_INCOMING_DIR=/amule/incoming \
  -v amularr-data:/data -v /srv/amule/incoming:/amule/incoming \
  ghcr.io/manuel-alcocer/amularr:0.2.0
```

Después añade `http://amularr:8080` como indexer *Generic Torznab* en
Prowlarr y como cliente de descarga *qBittorrent* en Sonarr/Radarr, y fija
el *Download Client* del indexer a ese cliente.

## Desarrollo

```bash
pip install -e .[dev]
pytest
```

Build multiarch local:

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t ghcr.io/manuel-alcocer/amularr:dev .
```

### Publicar una versión

El workflow de GitHub Actions en `.github/workflows/build.yml` ejecuta los
tests en cada push y pull request, construye la imagen para `linux/amd64` y
`linux/arm64` y la publica en GHCR en los push a `main` (etiqueta `main`) y
en los tags `v*` (etiquetas `X.Y.Z`, `X.Y`, `latest`). Una release es:

```bash
# sube la versión en pyproject.toml y amularr/__init__.py, haz commit y
git tag v0.2.0 && git push origin main v0.2.0
```

El workflow rechaza un tag que no coincida con `amularr.__version__`.

`amularr/ec/` es una implementación independiente del protocolo EC
(framing de paquetes, números UTF-8, zlib, handshake MD5 con sal) que puede
reutilizarse en otras herramientas para aMule.
