# Uso: alta en Prowlarr, Sonarr y Radarr

*[English](en/usage.md) · Español*

amularr expone dos fachadas en un mismo puerto:

| Ruta | Habla | Se da de alta en |
| --- | --- | --- |
| `/api` | Torznab | Prowlarr, como indexer |
| `/api/v2/...` | API web de qBittorrent v2 | Sonarr y Radarr, como cliente de descarga |

Cada resultado ed2k se convierte en un torrent falso cuyo info hash lleva
dentro el hash ed2k, de modo que una release "capturada" desde el indexer
puede devolverse a la fachada de cliente de descarga, que la convierte en un
enlace `ed2k://` para aMule. Por eso hay que dar de alta las dos mitades, y
las capturas del indexer amularr tienen que ir al cliente de descarga
amularr (ver *Enrutado*).

## 1. Prowlarr: el indexer

*Indexers → Add Indexer → Generic Torznab*:

| Campo | Valor |
| --- | --- |
| Name | `aMule (amularr)` |
| URL | `http://amularr:8080` (o `http://amularr.<namespace>.svc:8080`) |
| API Path | `/api` |
| API Key | el valor de `AMULARR_API_KEY`, o vacío |
| Categories | Movies y TV (las caps anuncian 2000/2030/2040/2045 y 5000/5030/5040/5045) |
| Sync | *Full Sync* hacia Sonarr y Radarr |

*Test* lanza una búsqueda sin palabras clave; amularr la responde con los
resultados de búsquedas recientes o con la búsqueda `AMULARR_RSS_QUERY`
(`1080p` por defecto), así que el test nunca falla por un feed vacío.

Las búsquedas se traducen así:

| Petición de Prowlarr | Búsquedas ed2k |
| --- | --- |
| `t=tvsearch&q=Dark&season=3&ep=3` | `Dark S03E03`, `Dark 3x03` |
| `t=tvsearch&q=Dark&season=3` | `Dark S03`, `Dark temporada 3` |
| `t=movie&q=Blade Runner&year=1982` | `Blade Runner 1982`, `Blade Runner` |
| `t=search&q=...` | la consulta tal cual |

Sonarr y Radarr envían todos los títulos que conocen (original, scene y
alternativos), así que las releases en castellano con el título local
también se encuentran. Los resultados se filtran por extensiones de vídeo
en las categorías de series/películas, se ordenan por número de fuentes
(que se comunica como seeders) y se clasifican en SD/HD/UHD por su nombre.

## 2. Sonarr y Radarr: el cliente de descarga

*Settings → Download Clients → Add → qBittorrent*:

| Campo | Valor |
| --- | --- |
| Name | `aMule (amularr)` |
| Host / Port | `amularr` / `8080` (sin SSL, sin URL base) |
| Username / Password | solo si `AMULARR_QBT_USERNAME` / `AMULARR_QBT_PASSWORD` están definidas |
| Category | `tv-sonarr` / `radarr` (cualquier nombre; amularr guarda las categorías en su fichero de estado) |
| Priority | peor que tu cliente torrent real si tienes uno (ver *Enrutado*) |
| Remove Completed | activado: Sonarr/Radarr borran la entrada (y el fichero, si se lo piden) tras importar |

El cliente comunica:

- `downloading`, `stalledDL` (sin fuentes), `pausedDL`, `queuedDL` mientras
  aMule trabaja con el fichero;
- `pausedUP` con `progress = 1` cuando el fichero está completo en Incoming,
  para que la app *arr lo importe y luego lo elimine;
- `error` cuando aMule rechazó o perdió el enlace.

`content_path` es `AMULARR_INCOMING_DIR/<nombre del fichero>`; ese directorio
tiene que ser accesible para Sonarr/Radarr en esa misma ruta (o mediante un
remote path mapping para este cliente).

## 3. Enrutado: que las capturas de amularr no vayan a un qBittorrent real

Sonarr y Radarr eligen el cliente de descarga por *protocolo y prioridad*,
no por indexer. Si hay un qBittorrent real con mejor prioridad, todos los
magnets del indexer amularr van a parar allí y se quedan en `metaDL` (esos
hashes falsos terminan en `00000000`).

El arreglo está en el **indexer**: en Sonarr/Radarr *Settings → Indexers*,
abre `aMule (amularr) (Prowlarr)` y pon en *Download Client* el cliente
`aMule (amularr)`. Prowlarr conserva ese campo entre sincronizaciones. No
uses etiquetas del cliente de descarga para esto: casan con las etiquetas de
series/películas, no con los indexers.

Por API es `downloadClientId` en `/api/v3/indexer/<id>`.

## 4. Búsquedas de pendientes (capturas automáticas)

ed2k/Kad no tiene un feed de "novedades", así que la respuesta RSS por sí
sola nunca puede sacar a la luz un capítulo recién emitido, y Sonarr/Radarr
solo capturan automáticamente lo que aparece en su RSS sync. Para que las
capturas automáticas funcionen, dale a amularr acceso de lectura a las apps
*arr:

```
AMULARR_SONARR_URL=http://sonarr:8989   AMULARR_SONARR_API_KEY=<Settings → General → API Key>
AMULARR_RADARR_URL=http://radarr:7878   AMULARR_RADARR_API_KEY=<Settings → General → API Key>
```

Con eso, cada petición RSS de Sonarr/Radarr hace que amularr, en segundo
plano:

1. lea *Wanted → Missing* (elementos monitorizados emitidos, estrenados o
   añadidos dentro de `AMULARR_WANTED_DAYS`, 7 por defecto);
2. lance las mismas búsquedas que haría la app *arr (`Serie S01E05` y
   `Serie 1x05` con cada título conocido; `Película 2022` con el título, el
   título original y los alternativos en `AMULARR_WANTED_TITLE_LANGUAGES`);
3. conserve los resultados en el feed hasta que el elemento deje de estar
   pendiente.

El siguiente RSS sync (cada 15 minutos por defecto en Sonarr/Radarr) ve la
release y la captura si pasa el perfil de calidad. Un elemento pendiente se
vuelve a buscar cada `AMULARR_WANTED_RESEARCH_INTERVAL` segundos (6 horas),
así que una release que aparece en la red un día después también se recoge.
Cada ciclo ejecuta como mucho `AMULARR_WANTED_MAX_SEARCHES` búsquedas (30), y
cada búsqueda tarda hasta `AMULARR_SEARCH_TIMEOUT` segundos, de modo que una
lista larga se va procesando a lo largo de varios syncs.

El log muestra lo que ha pasado:

```
wanted tv: Lanterns S01E05 -> 6 results (Lanterns S01E05, Lanterns 1x05, Linternas S01E05, Linternas 1x05)
wanted tv: 1 items wanted, 1 searched now (6 hits), 0 postponed
```

## 5. Verlo funcionar

- `GET /health` → versión de aMule, estado de conexión ed2k/Kad, `ok`.
- Logs: cada petición Torznab con su consulta, categorías y número de
  resultados; cada búsqueda EC con duración y resultados; cada alta o
  borrado enviado a aMule.
- *Activity → Queue* de Sonarr/Radarr muestra las descargas de aMule con el
  progreso y el número de fuentes.

## Resolución de problemas

**El test de Prowlarr falla / feed vacío.** Mira `/health`; si aMule no está
conectado a ed2k ni a Kad, toda búsqueda devuelve nada. Pon en
`AMULARR_RSS_QUERY` algo común en tu idioma si el `1080p` por defecto no
encuentra nada.

**Las capturas acaban en el qBittorrent real.** Ver *Enrutado*. Borra allí
las entradas falsas (hash terminado en `00000000`) tras arreglar el cliente
de descarga del indexer.

**Sonarr no importa nada cuando aMule termina.** `content_path` no es
accesible desde Sonarr: monta Incoming en la misma ruta o añade un remote
path mapping. La cola de Sonarr muestra la ruta exacta que ha intentado.

**La descarga desaparece de la cola tras reiniciar aMule.** aMule olvida los
ficheros completados al reiniciar. amularr guarda su propio estado y
comprueba Incoming en disco, pero ese fichero tiene que sobrevivir a los
reinicios (`AMULARR_STATE_FILE` en un volumen).

**Sonarr avisa "rss sync didn't cover the period".** Desde 0.2.0 los ítems
llevan como `pubDate` la hora en que se vieron por primera vez, lo que
elimina el aviso; las versiones anteriores fechaban todo "ahora".
