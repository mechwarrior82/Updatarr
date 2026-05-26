import docker
import tarfile
import os
import logging
import time
from datetime import datetime
from pathlib import Path

client = docker.from_env()
logger = logging.getLogger(__name__)

BACKUP_ROOT = os.environ.get("BACKUP_ROOT", "/backups")


# ─────────────────────────────────────────────
# Image helpers
# ─────────────────────────────────────────────

def get_current_image_id(container_name: str) -> str | None:
    try:
        c = client.containers.get(container_name)
        return c.image.id
    except docker.errors.NotFound:
        return None


def pull_latest_image(image_name: str) -> docker.models.images.Image:
    logger.info(f"Pulling latest image: {image_name}")
    return client.images.pull(image_name)


# ─────────────────────────────────────────────
# Volume backup / restore
# ─────────────────────────────────────────────

def backup_volumes(container_name: str, tag: str, stop_first: bool = False) -> list[str]:
    """
    Back up every named volume attached to a container.

    stop_first=True  — stops the container before backup and restarts after.
                       Guarantees a clean, consistent database snapshot.
                       Used automatically for pre-update backups.
    stop_first=False — backs up live. Convenient for scheduled/manual backups
                       but SQLite WAL files may cause minor warnings.

    Returns list of backup file paths created.
    """
    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        logger.warning(f"Container {container_name} not found for backup.")
        return []

    was_running = container.status == "running"

    if stop_first and was_running:
        logger.info(f"Stopping {container_name} for clean backup...")
        container.stop(timeout=30)
        container.reload()

    backup_dir = Path(BACKUP_ROOT) / container_name
    backup_dir.mkdir(parents=True, exist_ok=True)

    backed_up = []
    mounts = container.attrs.get("Mounts", [])

    for mount in mounts:
        if mount.get("Type") != "volume":
            continue
        volume_name = mount["Name"]
        archive_name = f"{volume_name}_{tag}.tar.gz"
        archive_path = backup_dir / archive_name

        logger.info(f"Backing up volume {volume_name} -> {archive_path}")

        # debian:bookworm-slim for GNU tar (alpine BusyBox tar lacks --ignore-failed-read)
        try:
            client.containers.run(
                "debian:bookworm-slim",
                command=f"tar czf /backup/{archive_name} --ignore-failed-read -C /source .",
                volumes={
                    volume_name: {"bind": "/source", "mode": "ro"},
                    str(backup_dir): {"bind": "/backup", "mode": "rw"},
                },
                remove=True,
            )
            backed_up.append(str(archive_path))
            logger.info(f"Volume backup complete: {archive_path}")
        except Exception as e:
            logger.error(f"Failed to back up volume {volume_name}: {e}")

    if stop_first and was_running:
        logger.info(f"Restarting {container_name} after backup...")
        container.start()

    return backed_up


def restore_volumes(container_name: str, tag: str) -> bool:
    """
    Restore volumes for a container from a backup tag.
    """
    backup_dir = Path(BACKUP_ROOT) / container_name
    if not backup_dir.exists():
        logger.error(f"No backup directory found for {container_name}")
        return False

    archives = list(backup_dir.glob(f"*_{tag}.tar.gz"))
    if not archives:
        logger.error(f"No backups found for tag {tag} in {backup_dir}")
        return False

    for archive_path in archives:
        # Volume name is everything before _{tag}.tar.gz
        volume_name = archive_path.name.replace(f"_{tag}.tar.gz", "")
        logger.info(f"Restoring volume {volume_name} from {archive_path}")

        try:
            # Ensure volume exists
            try:
                client.volumes.get(volume_name)
            except docker.errors.NotFound:
                client.volumes.create(name=volume_name)

            client.containers.run(
                "debian:bookworm-slim",
                command=f"sh -c 'rm -rf /target/* && tar xzf /backup/{archive_path.name} -C /target'",
                volumes={
                    volume_name: {"bind": "/target", "mode": "rw"},
                    str(backup_dir): {"bind": "/backup", "mode": "ro"},
                },
                remove=True,
            )
            logger.info(f"Restored volume {volume_name}")
        except Exception as e:
            logger.error(f"Failed to restore volume {volume_name}: {e}")
            return False

    return True


def list_backups(container_name: str) -> list[dict]:
    """Return a list of backup tags available for a container."""
    import re
    backup_dir = Path(BACKUP_ROOT) / container_name
    if not backup_dir.exists():
        return []

    # Tag format is always YYYYMMDD_HHMMSS — match it directly
    tag_pattern = re.compile(r'(\d{8}_\d{6})\.tar\.gz$')

    tags: dict[str, dict] = {}
    for f in backup_dir.glob("*.tar.gz"):
        m = tag_pattern.search(f.name)
        if not m:
            continue
        tag = m.group(1)
        if tag not in tags:
            tags[tag] = {
                "tag": tag,
                "files": [],
                "created": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
            }
        tags[tag]["files"].append(f.name)

    return sorted(tags.values(), key=lambda x: x["created"], reverse=True)


# ─────────────────────────────────────────────
# Container lifecycle
# ─────────────────────────────────────────────

def get_container_config(container_name: str) -> dict:
    """Extract the config needed to recreate a container."""
    c = client.containers.get(container_name)
    return c.attrs


def stop_container(container_name: str):
    try:
        c = client.containers.get(container_name)
        c.stop(timeout=30)
        logger.info(f"Stopped {container_name}")
    except docker.errors.NotFound:
        pass


def remove_container(container_name: str):
    try:
        c = client.containers.get(container_name)
        c.remove(force=True)
        logger.info(f"Removed {container_name}")
    except docker.errors.NotFound:
        pass


def recreate_with_new_image(container_name: str, new_image: str) -> bool:
    """
    Stop, remove, and recreate a container with a new image.
    NOTE: For compose-managed containers this sends a restart signal;
    the compose file is the source of truth for full config.
    We use `docker compose up -d --no-deps <service>` via subprocess for safety.
    """
    import subprocess
    compose_file = os.environ.get("COMPOSE_FILE", "/compose/docker-compose.yml")
    project = os.environ.get("COMPOSE_PROJECT", "arr")

    try:
        result = subprocess.run(
            ["docker", "compose", "-f", compose_file, "-p", project,
             "up", "-d", "--no-deps", "--pull", "always", container_name],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            logger.error(f"Compose up failed: {result.stderr}")
            return False
        logger.info(f"Recreated {container_name} with latest image")
        return True
    except Exception as e:
        logger.error(f"Failed to recreate {container_name}: {e}")
        return False


def recreate_with_image_id(container_name: str, image_id: str) -> bool:
    """Roll back a container to a specific image ID."""
    import subprocess
    compose_file = os.environ.get("COMPOSE_FILE", "/compose/docker-compose.yml")
    project = os.environ.get("COMPOSE_PROJECT", "arr")

    try:
        # Tag the old image so compose can reference it
        old_image = client.images.get(image_id)
        old_image.tag(f"{container_name}-rollback", "latest")

        result = subprocess.run(
            ["docker", "compose", "-f", compose_file, "-p", project,
             "up", "-d", "--no-deps", container_name],
            capture_output=True, text=True, timeout=300,
            env={**os.environ, "COMPOSE_FORCE_NEW_IMAGE": image_id}
        )
        return result.returncode == 0
    except Exception as e:
        logger.error(f"Rollback failed for {container_name}: {e}")
        return False


# ─────────────────────────────────────────────
# Health verification
# ─────────────────────────────────────────────

def wait_for_health(container_name: str, timeout: int = 90) -> bool:
    """
    Wait for a container to report healthy or running (if no healthcheck defined).
    Returns True on success, False on timeout or unhealthy.
    """
    deadline = time.time() + timeout
    logger.info(f"Waiting up to {timeout}s for {container_name} to become healthy...")

    while time.time() < deadline:
        try:
            c = client.containers.get(container_name)
            c.reload()
            status = c.status

            if status != "running":
                time.sleep(3)
                continue

            health = c.attrs.get("State", {}).get("Health")
            if health is None:
                # No healthcheck defined — running is good enough
                logger.info(f"{container_name} is running (no healthcheck defined)")
                return True

            health_status = health.get("Status")
            if health_status == "healthy":
                logger.info(f"{container_name} is healthy")
                return True
            elif health_status == "unhealthy":
                logger.error(f"{container_name} reported unhealthy")
                return False

        except docker.errors.NotFound:
            logger.error(f"{container_name} not found during health check")
            return False

        time.sleep(5)

    logger.error(f"{container_name} health check timed out after {timeout}s")
    return False


# ─────────────────────────────────────────────
# Container inventory
# ─────────────────────────────────────────────

def list_all_containers() -> list[dict]:
    containers = client.containers.list(all=True)
    result = []
    for c in containers:
        c.reload()
        health = c.attrs.get("State", {}).get("Health")
        result.append({
            "name": c.name,
            "status": c.status,
            "image": c.image.tags[0] if c.image.tags else c.image.short_id,
            "image_id": c.image.id,
            "health": health.get("Status") if health else None,
            "started": c.attrs["State"].get("StartedAt"),
        })
    return result

# ─────────────────────────────────────────────
# Update check
# ─────────────────────────────────────────────

def check_for_update(image_tag: str) -> dict:
    """
    Compare the local image digest against the remote registry digest.
    Returns {"up_to_date": bool, "local_digest": str, "remote_digest": str}
    Works for Docker Hub images (most *arr images). May not work for ghcr.io
    images that require auth.
    """
    import subprocess
    try:
        # Pull the manifest digest without downloading the full image
        result = subprocess.run(
            ["docker", "manifest", "inspect", "--verbose", image_tag],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return {"up_to_date": None, "error": "Could not reach registry"}

        import json
        data = json.loads(result.stdout)

        # Handle both single manifest and manifest list responses
        if isinstance(data, list):
            # Manifest list — find the amd64/linux entry
            remote_digest = None
            for entry in data:
                platform = entry.get("Descriptor", {}).get("platform", {})
                if platform.get("architecture") == "amd64" and platform.get("os") == "linux":
                    remote_digest = entry.get("Descriptor", {}).get("digest")
                    break
        else:
            remote_digest = data.get("Descriptor", {}).get("digest")

        if not remote_digest:
            return {"up_to_date": None, "error": "Could not parse remote digest"}

        # Get local image digest
        try:
            local_image = client.images.get(image_tag)
            local_digest = local_image.attrs.get("RepoDigests", [None])[0]
            if local_digest:
                # RepoDigests format: "image@sha256:abc123"
                local_digest = local_digest.split("@")[-1]
        except docker.errors.ImageNotFound:
            return {"up_to_date": None, "error": "Image not found locally"}

        up_to_date = local_digest == remote_digest
        return {
            "up_to_date": up_to_date,
            "local_digest": local_digest[:19] if local_digest else None,
            "remote_digest": remote_digest[:19] if remote_digest else None,
        }

    except Exception as e:
        logger.error(f"Update check failed for {image_tag}: {e}")
        return {"up_to_date": None, "error": str(e)}