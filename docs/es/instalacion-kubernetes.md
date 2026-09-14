# Instalación en Kubernetes

*[English](../install-kubernetes.md) · Español*

`deploy/kubernetes/amularr.yaml` contiene un ejemplo completo: un Secret con
la contraseña EC, un PVC de 100 Mi para el fichero de estado, el Deployment y
un Service ClusterIP. Da por hecho que aMule ya corre en algún sitio
accesible (un NAS, otro namespace) y que su directorio Incoming está
disponible como volumen.

## 1. Ajustar el manifiesto

```bash
cp deploy/kubernetes/amularr.yaml amularr.yaml
```

Edita:

- `metadata.namespace` en todos los objetos (el ejemplo usa `media`).
- `AMULE_EC_HOST` / `AMULE_EC_PORT`: dónde escucha aMule las External
  Connections. aMule dentro del clúster: el nombre de su Service. NAS: su
  IP.
- `ec_password_md5` en el Secret: la línea `Password=` del `remote.conf` de
  aMule, o la salida de `echo -n 'contraseña' | md5sum`. Para usar la
  contraseña en claro, pon `AMULE_EC_PASSWORD` y quita la variable MD5.
- El volumen `amule-incoming`: lo que le dé al pod el directorio Incoming de
  aMule (export NFS, hostPath, PVC RWX). Monta lo mismo en la misma ruta en
  los pods de Sonarr y Radarr, o configura en ellos un remote path mapping.
- `securityContext`: uid/gid iguales a los pods *arr, para que los ficheros
  que amularr borra tras una importación sean suyos.
- `storageClassName` del PVC si tu clúster no tiene clase por defecto.

Opcional: `sonarr_api_key` / `radarr_api_key` en el Secret activan las
búsquedas de pendientes (ver [uso.md](uso.md)). Se leen con
`optional: true`, así que el pod arranca sin ellas.

## 2. Aplicar

```bash
kubectl apply -f amularr.yaml
kubectl -n media rollout status deploy/amularr
kubectl -n media logs deploy/amularr
```

El log termina con `listening on 0.0.0.0:8080` y, si las claves están
puestas, `wanted-list searches enabled for: tv, movies`.

## 3. Verificar desde dentro del clúster

```bash
kubectl -n media run curl --rm -it --image=curlimages/curl --restart=Never -- \
  sh -c 'curl -s http://amularr:8080/health; echo; curl -s "http://amularr:8080/api?t=caps" | head -c 300'
```

Apunta Prowlarr a `http://amularr.<namespace>.svc:8080` y el cliente de
descarga de Sonarr/Radarr al host `amularr.<namespace>.svc`, puerto `8080`
([uso.md](uso.md)).

## Secretos gestionados fuera

Con External Secrets Operator (o sealed-secrets, SOPS, ...), sustituye el
Secret plano por tu propio objeto que genere un Secret llamado `amularr` con
las claves `ec_password_md5`, `sonarr_api_key` y `radarr_api_key`. El resto
del manifiesto no cambia.

## Actualizar

Cambia la etiqueta de la imagen y vuelve a aplicar. El Deployment usa la
estrategia `Recreate` porque dos réplicas se pelearían por el fichero de
estado; el despliegue tarda unos segundos y las descargas en curso siguen
en aMule mientras tanto.

## Probes

- `/ping` nunca toca aMule y sostiene la liveness probe.
- `/health` conecta con aMule y responde 503 mientras el enlace EC está
  caído; solo sostiene la readiness probe, así que un reinicio del NAS o de
  aMule saca a amularr del Service sin reiniciar el pod en bucle.
