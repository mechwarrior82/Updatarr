# Updatarr

A self-hosted container lifecycle manager for Docker. Works with any container configuration — compose stacks, multiple stacks, or standalone containers.

## Features

- Per-container monitoring toggle (checkbox in the UI).
- Automatic pre-update volume backups.
- Health-verified updates — rolls back automatically on failure.
- Hold system: a failed update sets a hold and blocks future auto-updates until you manually review and release.
- Manual backup and restore from the UI.
- Scheduled auto-updates (cron syntax via env var).
- Audit log of every update event.
- Compose stack detection — containers are automatically labelled with their stack name.

---

## Setup

### 1. Configure the compose file

Copy `docker-compose.yml.example` to `docker-compose.yml` and edit it:

```bash
cp docker-compose.yml.example docker-compose.yml
```

Set your backup path and timezone. No compose file mounting is needed — Updatarr reads container metadata directly from Docker.

### 2. Build and start

```bash
docker compose up -d --build
```

### 3. Open the UI

```
http://<your-server-ip>:3001
```

### 4. Enable monitoring

Check the checkbox next to each container you want managed. Unchecked containers are never auto-updated or backed up.

---

## Environment Variables

| Variable          | Default                  | Description                                 |
|-------------------|--------------------------|---------------------------------------------|
| `DB_PATH`         | `/app/config/manager.db` | SQLite database path                        |
| `BACKUP_ROOT`     | `/backups`               | Root directory for volume backups           |
| `UPDATE_SCHEDULE` | `0 4 * * *`              | Cron schedule for auto-updates              |

---

## Update Flow

```
1. Pre-flight: skip if container is held or not monitored
2. Backup all named volumes → /backups/<container_name>/<volume>_<tag>.tar.gz
3. docker pull <image>
4. Stop, remove, and recreate the container with the new image
   (full config — volumes, ports, networks, env vars — is preserved from the running container)
5. Wait for healthcheck to pass (configurable per container, default 90s)
   └─ SUCCESS → log it, done
   └─ FAILURE → restore volume backup
              → recreate with previous image ID
              → set HELD = true with reason
              → log rolled_back event
```

## How It Works With Your Stacks

Updatarr talks to Docker via the socket and reads container metadata directly. No compose files need to be mounted. When a container is updated, the existing running config (bind mounts, named volumes, port bindings, networks, environment variables, labels, etc.) is captured from Docker inspect and preserved in the recreated container.

Docker Compose labels (`com.docker.compose.project`, `com.docker.compose.service`) are read automatically to group containers by stack in the UI. Standalone containers (not managed by compose) are shown with a "standalone" badge.

## Gluetun Safety

The update runner always processes `gluetun` **last**. All containers using
`network_mode: "service:gluetun"` go offline when gluetun is updated, so
updating it last minimises disruption.

---

## Cloudflare Tunnel

Add to your Cloudflare tunnel dashboard if you want external access:

| Subdomain                        | Service URL             |
|----------------------------------|-------------------------|
| `updatarr.yourdomain.com`        | `http://10.0.0.1:3001`  |

Protect it with a Cloudflare Access policy — Updatarr has full access to your Docker socket.
