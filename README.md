# Updatarr

A self-hosted container lifecycle manager built for your *arr stack.

## Features

- Per-container monitoring toggle (checkbox in the UI).
- Automatic pre-update volume backups.
- Health-verified updates — rolls back automatically on failure.
- Hold system: a failed update sets a hold and blocks future auto-updates until you manually review and release.
- Manual backup and restore from the UI.
- Scheduled auto-updates (cron syntax via env var).
- Audit log of every update event.

---

## Setup

### 1. Update the compose file

Edit `docker-compose.yml` and set:

```yaml
- /path/to/your/arr/docker-compose.yml:/compose/docker-compose.yml:ro
- COMPOSE_PROJECT=arr   # Match your actual compose project name
```

To find your compose project name:
```bash
docker compose ls
```

### 2. Build and start

```bash
docker compose up -d --build
```

### 3. Open the UI

```
http://<your-server-ip>:3001
```

Or via Cloudflare tunnel at `updatarr.yourdomain.com`.

### 4. Enable monitoring

Check the checkbox next to each container you want managed. Unchecked containers are never auto-updated or backed up.

---

## Environment Variables

| Variable          | Default                        | Description                                 |
|-------------------|--------------------------------|---------------------------------------------|
| `DB_PATH`         | `/app/config/manager.db`       | SQLite database path                        |
| `BACKUP_ROOT`     | `/backups`                     | Root directory for volume backups           |
| `COMPOSE_FILE`    | `/compose/docker-compose.yml`  | Path to your arr stack compose file         |
| `COMPOSE_PROJECT` | `arr`                          | Docker Compose project name                 |
| `UPDATE_SCHEDULE` | `0 4 * * *`                    | Cron schedule for auto-updates              |

---

## Update Flow

```
1. Pre-flight: skip if container is held or not monitored
2. Backup all named volumes → /backups/<container_name>/<volume>_<tag>.tar.gz
3. docker compose up -d --no-deps --pull always <container>
4. Wait for healthcheck to pass (configurable per container, default 90s)
   └─ SUCCESS → log it, done
   └─ FAILURE → restore volume backup
              → recreate with previous image ID
              → set HELD = true with reason
              → log rolled_back event
```

## Gluetun Safety

The update runner always processes `gluetun` **last**. All containers using
`network_mode: "service:gluetun"` go offline when gluetun is updated, so
updating it last minimises disruption.

---

## Adding to Your Arr Compose

Add the service block from this project's `docker-compose.yml` into your main
arr `docker-compose.yml`, or run it as a separate stack — either works since
it connects to the external `serverNetwork`.

Add to your Cloudflare tunnel dashboard if you want external access:

| Subdomain                        | Service URL             |
|----------------------------------|-------------------------|
| `updatarr.yourdomain.com`        | `http://10.0.0.1:3001`  |

Protect it with a Cloudflare Access policy — Updatarr has full access to your Docker socket.
