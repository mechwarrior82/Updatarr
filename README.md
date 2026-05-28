# Updatarr

A self-hosted Docker container lifecycle manager. Updatarr keeps your containers up to date automatically — pulling new images, verifying health, rolling back on failure, and backing up volumes before every update. Works with any Docker setup: Compose stacks (including multiple stacks, Portainer-managed), and standalone containers.

---

## Features

- **Universal** — works with any container, any Compose stack, or standalone containers. No compose file mounting required.
- **Auto-update** — scheduled image pulls with health-verified recreation.
- **Rollback** — if a container fails its health check after update, volumes are restored and the old image is redeployed automatically.
- **Pre-update backups** — named volumes and bind mounts are archived before every update.
- **Hold system** — a failed update puts the container on hold and blocks future auto-updates until you review and release it.
- **Manual controls** — trigger updates, backups, and restores from the UI at any time.
- **Audit log** — every update attempt is recorded with timestamps, old/new image IDs, and outcome.
- **Stack detection** — Compose project/service labels are read automatically so containers are grouped by stack in the UI.
- **Config preservation** — full container configuration is captured and replayed on recreation: volumes, ports, networks, static IPs, environment variables, labels, capabilities, GPU runtime, CPU/memory limits, restart policy, and more.

---

## Prerequisites

- Docker and Docker Compose installed on your server
- A directory on your server (or NAS) to store backups
- The Docker socket accessible at `/var/run/docker.sock`

---

## Setup

### 1. Get the files

Clone the repo or download and extract it to your server:

```bash
git clone https://github.com/mechwarrior82/updatarr.git
cd updatarr
```

### 2. Configure docker-compose.yml

Copy the example and edit it:

```bash
cp docker-compose.yml.example docker-compose.yml
```

Open `docker-compose.yml` and update the following:

**Backup path** — replace `/mnt/nas/backups/docker` with a real path on your server where backups will be stored:
```yaml
volumes:
  - /your/backup/path:/backups
```

**Timezone** — set your local timezone:
```yaml
environment:
  - TZ=America/New_York
```

**Update schedule** — cron expression for auto-updates (default is 4 AM daily):
```yaml
  - UPDATE_SCHEDULE=0 4 * * *
```

**Network** (optional) — if you use a custom Docker network with a static IP, update the network section to match. If you just want Updatarr on the default bridge, remove the `networks:` block entirely:
```yaml
networks:
  yourNetworkName:
    ipv4_address: 10.0.0.x
```

### 3. Build and start

```bash
docker compose up -d --build
```

### 4. Open the UI

```
http://<your-server-ip>:3001
```

### 5. Enable monitoring

In the UI, check the checkbox next to each container you want Updatarr to manage. Unchecked containers are never auto-updated or backed up. You can also click into any container to configure per-container options (see [Per-Container Settings](#per-container-settings) below).

---

## Environment Variables

| Variable          | Default                  | Description                                              |
|-------------------|--------------------------|----------------------------------------------------------|
| `TZ`              | *(unset)*                | Timezone for scheduled jobs (e.g. `America/Chicago`)     |
| `DB_PATH`         | `/app/config/manager.db` | SQLite database path — keep it inside the config volume  |
| `BACKUP_ROOT`     | `/backups`               | Root directory where volume backups are written          |
| `UPDATE_SCHEDULE` | `0 4 * * *`              | Cron schedule for automatic updates                      |

---

## Per-Container Settings

Click on any container row in the UI to open the detail panel. Settings available per container:

| Setting | Description |
|---|---|
| **Monitor** | Enable/disable auto-updates and backups for this container |
| **Health timeout** | Seconds to wait for a healthy status after recreation (default: 90s). Increase for slow-starting containers. |
| **Backup retention** | How many backups to keep per volume (default: 3). Older backups are deleted automatically. |
| **Excluded volumes** | Comma-separated substrings — any volume or bind mount whose name/path contains one of these is skipped during backup (e.g. `cache,transcode`). |
| **Notes** | Free-text notes visible in the UI. |
| **GitHub repo** | Optional — link a GitHub repo (e.g. `linuxserver/docker-sonarr`) to track semantic version numbers alongside image updates. |

---

## Update Flow

```
1. Pre-flight: skip if container is held or not monitored
2. Backup all volumes and bind mounts → /backups/<container_name>/
3. docker pull <image>
4. Stop, remove, and recreate the container with the new image
   (full config is captured from Docker inspect and preserved exactly)
5. Wait for healthcheck to pass (configurable timeout per container)
   └─ SUCCESS → log it, done
   └─ FAILURE → restore volume backups
              → recreate with previous image
              → set HELD = true with reason
              → log rolled_back event
```

---

## How It Works

Updatarr talks to Docker via the socket (`/var/run/docker.sock`) and reads container metadata directly using the Docker API. No compose files need to be mounted.

When a container is updated or rolled back, Updatarr captures the full configuration from `docker inspect` and replays it when creating the replacement container. This includes:

- Named volumes and bind mounts
- Port bindings
- Networks and static IP addresses
- Environment variables and labels
- Capabilities (`cap_add` / `cap_drop`), `privileged` mode
- GPU / custom runtime (e.g. `nvidia`)
- Restart policy, PID/IPC mode, sysctls
- CPU and memory limits
- Logging driver configuration
- Stop signal and health check settings

Docker Compose labels (`com.docker.compose.project`, `com.docker.compose.service`) are read automatically to group containers by stack in the UI. Standalone containers show a "standalone" badge.

---

## The Hold System

When a container auto-rolls back after a failed update, Updatarr sets a **hold** on it. A held container:

- Will not be auto-updated again
- Shows a hold banner in the UI with the reason
- Can only be updated again after you manually release the hold

To release a hold: open the container in the UI and click **Release Hold**. This is intentional — it forces you to review what happened before the next attempt.

---

## Backup Behaviour

Before every update, Updatarr stops the container, archives all its volumes, then restarts it (the backup step) before pulling the new image. This ensures a clean, consistent snapshot.

Backups are stored at:
```
/backups/<container_name>/<volume-or-path>_<timestamp>.tar.gz
```

Bind mounts from large storage paths (`/mnt`, `/media`, etc.) and system paths (`/proc`, `/sys`, `/dev`) are skipped automatically. You can add your own exclusions in the per-container settings.

Manual backups and restores are also available from the UI at any time.

---

## Special Cases

### Gluetun (VPN gateway)

Updatarr always processes `gluetun` **last**. Containers that use `network_mode: service:gluetun` lose network access when Gluetun is recreated, so updating it last minimises disruption to everything routing through it.

### Containers with static IPs

Static IP addresses assigned in Compose (`ipv4_address`) are read from the container's network inspect data and preserved exactly when the container is recreated. No manual configuration needed.

### GPU containers

If a container uses a custom Docker runtime (e.g. `runtime: nvidia` for GPU access), that runtime setting is captured and preserved on recreation. GPU containers will continue to have GPU access after an update.

### Shared network namespace (service:X / container:X)

Containers that share another container's network namespace (e.g. `network_mode: service:gluetun`) are handled correctly — Updatarr omits hostname and port binding settings that belong to the namespace owner.

---

## Optional: Cloudflare Tunnel

To access the UI remotely without opening a port, add it to your Cloudflare tunnel:

| Subdomain                        | Service URL             |
|----------------------------------|-------------------------|
| `updatarr.yourdomain.com`        | `http://<server-ip>:3001` |

Protect it with a Cloudflare Access policy — Updatarr has full access to your Docker socket and should not be exposed publicly without authentication.
