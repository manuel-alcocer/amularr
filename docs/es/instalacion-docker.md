# Instalación con Docker

*[English](../install-docker.md) · Español*

amularr es un único contenedor casi sin estado (un pequeño fichero JSON).
Necesita acceso de red a un demonio aMule y ver el directorio Incoming de
aMule. Hay imágenes para `linux/amd64` y `linux/arm64` en
`ghcr.io/manuel-alcocer/amularr`.

## 1. Preparar aMule

amularr habla con aMule por External Connections (EC), el mismo protocolo
que usan `amulecmd` y `amulegui`.

1. Habilita EC en `amule.conf` (o en las preferencias de aMule, *Controles
   remotos*):

   ```ini
   [ExternalConnect]
   AcceptExternalConnections=1
   ECPort=4712
   ECPassword=<md5 de tu contraseña>
   ```

   `ECPassword` es el MD5 hex de la contraseña:
   `echo -n 'mi-contraseña' | md5sum`. Reinicia aMule después.
2. Si aMule corre en Docker (por ejemplo `ngosang/amule`), publica el puerto
   4712 y apunta la ruta de su directorio Incoming en el host.
3. Prueba desde la máquina que va a ejecutar amularr:

   ```bash
   amulecmd -h <host-amule> -p 4712 -P 'mi-contraseña' -c status
   ```

## 2. Decidir las rutas

Sonarr y Radarr importan los ficheros terminados desde el directorio
Incoming de aMule, así que el mismo directorio tiene que verse desde tres
sitios:

| Quién | Montaje | Variable |
| --- | --- | --- |
| aMule | su directorio Incoming | |
| amularr | por ejemplo `/amule/incoming` | `AMULARR_LOCAL_INCOMING_DIR` |
| Sonarr / Radarr | por ejemplo `/amule/incoming` | `AMULARR_INCOMING_DIR` (lo que amularr les comunica) |

Usa la misma ruta en Sonarr/Radarr y en `AMULARR_INCOMING_DIR`, o añade un
*Remote Path Mapping* en Sonarr/Radarr para el cliente de descarga amularr.

## 3. Arrancar

### docker run

```bash
docker run -d --name amularr --restart unless-stopped \
  --user 1000:100 \
  -p 8080:8080 \
  -e AMULE_EC_HOST=192.168.1.12 \
  -e AMULE_EC_PASSWORD='mi-contraseña' \
  -e AMULARR_INCOMING_DIR=/amule/incoming \
  -e AMULARR_LOCAL_INCOMING_DIR=/amule/incoming \
  -v amularr-data:/data \
  -v /srv/amule/incoming:/amule/incoming \
  ghcr.io/manuel-alcocer/amularr:0.2.0
```

Arráncalo con el mismo uid/gid que tus contenedores de Sonarr/Radarr, para
que amularr pueda borrar un fichero ya importado cuando Sonarr se lo pida.

### docker compose

`deploy/docker-compose.yml` contiene la misma configuración con todas las
opciones comentadas, más un servicio aMule opcional (`--profile with-amule`)
para un stack que aún no tiene aMule:

```bash
cp deploy/docker-compose.yml ./docker-compose.yml
# edita las rutas, la contraseña EC y, si quieres, las API keys de los *arr
docker compose up -d
docker compose logs -f amularr
```

Si el fichero compose va a un repositorio, usa `AMULE_EC_PASSWORD_MD5` (la
línea `Password=` de `~/.aMule/remote.conf`) en vez de la contraseña en
claro.

## 4. Comprobar

```bash
curl -s http://localhost:8080/health | jq
curl -s 'http://localhost:8080/api?t=caps' | head
```

`/health` devuelve `"ok": true` y la versión de aMule en cuanto el enlace EC
está arriba. `/ping` responde `pong` sin tocar aMule (úsalo en los health
checks del contenedor). Después da de alta Prowlarr, Sonarr y Radarr como
se explica en [uso.md](uso.md).

## Actualizar

```bash
docker compose pull amularr && docker compose up -d amularr
```

El fichero de estado del volumen `amularr-data` es compatible hacia
delante; conserva el volumen entre actualizaciones o las descargas en curso
perderán su hash de torrent falso y Sonarr/Radarr las darán por eliminadas.
