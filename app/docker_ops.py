import docker
import tarfile
import os
import logging
import time
from datetime import datetime
from pathlib import Path
from docker.types import LogConfig, Ulimit

# Short timeout prevents a single unresponsive container from hanging
# the entire /api/containers request for 60 seconds
client = docker.from_env(timeout=10)
logger = logging.getLogger(__name__)

BACKUP_ROOT = os.environ.get("BACKUP_ROOT", "/backups")


def _resolve_host_path(container_path: str) -> str:
    """
    Translate a path inside the Updatarr container to the real path on the
    Docker host. Required because when we spin up a temporary debian container
    to tar a volume, Docker resolves bind-mount paths from the HOST — it has
    no knowledge of paths inside the Updatarr container.

    Looks at Updatarr's own mounts and finds the bind mount whose destination
    is a prefix of container_path, then remaps to the host source.
    """
    try:
        self_name = os.environ.get("HOSTNAME", "updatarr")
        self_container = client.containers.get(self_name)
        mounts = self_container.attrs.get("Mounts", [])
        container_path = str(container_path)

        # Find the deepest matching bind mount
        best_match = None
        best_len = 0
        for m in mounts:
            if m.get("Type") != "bind":
                continue
            dest = m["Destination"].rstrip("/")
            if container_path.startswith(dest) and len(dest) > best_len:
                best_match = m
                best_len = len(dest)

        if best_match:
            dest = best_match["Destination"].rstrip("/")
            source = best_match["Source"].rstrip("/")
            host_path = source + container_path[len(dest):]
            logger.debug(f"Resolved {container_path} -> {host_path}")
            return host_path

    except Exception as e:
        logger.warning(f"Could not resolve host path for {container_path}: {e}")

    # Fallback: return as-is (works if running with host network or direct bind)
    return str(container_path)


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

# Known cache/transient volume substrings that should never be backed up by default.
# These are fully regeneratable and often very large.
_DEFAULT_EXCLUDED_SUBSTRINGS = [
    "-cache",
    "_cache",
    "-transcode",
    "_transcode",
    "-transcodes",
]


def _enforce_retention(backup_dir: Path, volume_name: str, keep: int):
    """
    Delete old backups for a specific volume, keeping only the most recent `keep` copies.
    """
    pattern = f"{volume_name}_*.tar.gz"
    existing = sorted(backup_dir.glob(pattern), key=lambda f: f.stat().st_mtime, reverse=True)
    to_delete = existing[keep:]
    for f in to_delete:
        try:
            f.unlink()
            logger.info(f"Retention: deleted old backup {f.name}")
        except Exception as e:
            logger.warning(f"Retention: could not delete {f.name}: {e}")


def backup_volumes(
    container_name: str,
    tag: str,
    stop_first: bool = False,
    excluded_volumes: str = None,
    retention: int = 3,
) -> list[str]:
    """
    Back up every named volume attached to a container.

    stop_first=True      — stops the container before backup and restarts after.
                           Guarantees a clean, consistent database snapshot.
    excluded_volumes     — comma-separated substrings; any volume whose name contains
                           one of these strings is skipped. Defaults plus user-defined.
    retention            — keep only the last N backups per volume, delete older ones.

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

    # Build exclusion list — defaults + user-defined
    exclusions = list(_DEFAULT_EXCLUDED_SUBSTRINGS)
    if excluded_volumes:
        for e in excluded_volumes.split(","):
            e = e.strip()
            if e:
                exclusions.append(e)

    backed_up = []

    # Re-fetch attrs after potential stop to get current mount state
    container.reload()
    mounts = container.attrs.get("Mounts", [])

    named_volumes = [m for m in mounts if m.get("Type") == "volume"]
    bind_mounts = [m for m in mounts if m.get("Type") == "bind"]
    logger.info(
        f"[{container_name}] Found {len(mounts)} mounts total: "
        f"{len(named_volumes)} named volume(s), {len(bind_mounts)} bind mount(s)"
    )
    for m in named_volumes:
        logger.info(f"[{container_name}]   volume: {m['Name']} -> {m['Destination']}")
    for m in bind_mounts:
        logger.debug(f"[{container_name}]   bind: {m.get('Source','?')} -> {m['Destination']} (skipped)")

    host_backup_dir = _resolve_host_path(backup_dir)
    logger.debug(f"Host backup dir: {host_backup_dir}")

    for mount in mounts:
        if mount.get("Type") != "volume":
            continue
        volume_name = mount["Name"]

        # Check exclusions
        excluded_by = next((e for e in exclusions if e.lower() in volume_name.lower()), None)
        if excluded_by:
            logger.info(f"[{container_name}] Skipping volume {volume_name} (excluded by rule: {excluded_by!r})")
            continue

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
                    host_backup_dir: {"bind": "/backup", "mode": "rw"},
                },
                remove=True,
            )
            backed_up.append(str(archive_path))
            logger.info(f"Volume backup complete: {archive_path}")

            # Enforce retention — delete old backups beyond the limit
            _enforce_retention(backup_dir, volume_name, keep=retention)

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

            host_backup_dir = _resolve_host_path(backup_dir)
            client.containers.run(
                "debian:bookworm-slim",
                command=f"sh -c 'rm -rf /target/* && tar xzf /backup/{archive_path.name} -C /target'",
                volumes={
                    volume_name: {"bind": "/target", "mode": "rw"},
                    host_backup_dir: {"bind": "/backup", "mode": "ro"},
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



def _recreate_with_image(container_name: str, image: str) -> bool:
    """
    Stop, remove, and recreate a container with the given image, preserving
    all configuration (volumes, ports, networks, env vars, labels, etc.).
    Works for compose-managed and standalone containers alike — no compose
    file access required. The new container inherits the old container's full
    HostConfig so named volumes, bind mounts, ports, and network mode are all
    preserved exactly.
    """
    # Lifecycle ops (create, network connect, start) can take well over 10 s,
    # so use a dedicated client with a longer timeout than the global one used
    # for container listing.
    lc = docker.from_env(timeout=120)

    try:
        attrs = lc.api.inspect_container(container_name)
        config = attrs["Config"]
        hc_raw = attrs["HostConfig"]
        net_settings = attrs.get("NetworkSettings", {})

        # Stop if running (may already be stopped by backup step)
        try:
            c = lc.containers.get(container_name)
            if c.status == "running":
                c.stop(timeout=30)
        except docker.errors.NotFound:
            pass

        lc.api.remove_container(container_name, force=True)
        logger.info(f"Removed old container {container_name}")

        # Containers sharing another container's or the host's network namespace
        # cannot have their own hostname or port bindings — those belong to the
        # namespace owner (e.g. gluetun for service:gluetun dependents).
        net_mode = hc_raw.get("NetworkMode", "bridge")
        uses_shared_netns = net_mode.startswith(("host", "container:", "service:"))

        log_cfg_raw = hc_raw.get("LogConfig")
        log_cfg = (
            LogConfig(type=log_cfg_raw["Type"], config=log_cfg_raw.get("Config") or {})
            if log_cfg_raw else None
        )
        ulimits = [
            Ulimit(name=u["Name"], soft=u.get("Soft", 0), hard=u.get("Hard", 0))
            for u in (hc_raw.get("Ulimits") or [])
        ] or None

        hc = lc.api.create_host_config(
            binds=hc_raw.get("Binds") or [],
            port_bindings={} if uses_shared_netns else (hc_raw.get("PortBindings") or {}),
            restart_policy=hc_raw.get("RestartPolicy") or {},
            network_mode=net_mode,
            volumes_from=hc_raw.get("VolumesFrom") or [],
            cap_add=hc_raw.get("CapAdd"),
            cap_drop=hc_raw.get("CapDrop"),
            devices=hc_raw.get("Devices") or [],
            privileged=hc_raw.get("Privileged", False),
            pid_mode=hc_raw.get("PidMode") or "",
            ipc_mode=hc_raw.get("IpcMode") or "",
            dns=hc_raw.get("Dns") or [],
            dns_search=hc_raw.get("DnsSearch") or [],
            extra_hosts=hc_raw.get("ExtraHosts") or [],
            group_add=hc_raw.get("GroupAdd") or [],
            read_only=hc_raw.get("ReadonlyRootfs", False),
            security_opt=hc_raw.get("SecurityOpt") or [],
            sysctls=hc_raw.get("Sysctls") or {},
            log_config=log_cfg,
            shm_size=hc_raw.get("ShmSize"),
            tmpfs=hc_raw.get("Tmpfs") or {},
            ulimits=ulimits,
            mem_limit=hc_raw.get("Memory") or 0,
            memswap_limit=hc_raw.get("MemorySwap") or 0,
            cpu_shares=hc_raw.get("CpuShares") or 0,
            cpuset_cpus=hc_raw.get("CpusetCpus") or "",
        )

        cid = lc.api.create_container(
            image=image,
            name=container_name,
            command=config.get("Cmd"),
            hostname="" if uses_shared_netns else (config.get("Hostname") or ""),
            user=config.get("User") or "",
            environment=config.get("Env") or [],
            volumes=list((config.get("Volumes") or {}).keys()),
            ports=[] if uses_shared_netns else list((config.get("ExposedPorts") or {}).keys()),
            labels=config.get("Labels") or {},
            working_dir=config.get("WorkingDir") or "",
            entrypoint=config.get("Entrypoint"),
            host_config=hc,
        )

        # Reconnect to additional networks beyond the primary NetworkMode.
        # Skip for shared-netns modes — networking is inherited from the owner.
        if not uses_shared_netns and net_mode not in ("host", "none"):
            for net_name, net_cfg in net_settings.get("Networks", {}).items():
                if net_name in (net_mode, "bridge"):
                    continue
                try:
                    net = lc.networks.get(net_name)
                    net.connect(cid["Id"], aliases=net_cfg.get("Aliases") or [])
                    logger.info(f"Connected {container_name} to network {net_name}")
                except Exception as e:
                    logger.warning(f"Could not connect {container_name} to network {net_name}: {e}")

        lc.api.start(cid)
        logger.info(f"Recreated {container_name} with image {image[:40]}")
        return True

    except Exception as e:
        logger.error(f"Failed to recreate {container_name}: {e}")
        return False


def recreate_with_new_image(container_name: str, _: str) -> bool:
    """
    Pull the latest version of the container's current image and recreate it.
    Works for any container — compose-managed or standalone — without needing
    access to the compose file on disk.
    """
    import subprocess

    try:
        attrs = client.api.inspect_container(container_name)
        image_ref = attrs["Config"]["Image"]
    except Exception as e:
        logger.error(f"Could not inspect {container_name}: {e}")
        return False

    logger.info(f"Pulling latest image: {image_ref}")
    result = subprocess.run(
        ["docker", "pull", image_ref],
        capture_output=True, text=True, timeout=300
    )
    if result.returncode != 0:
        logger.error(f"Pull failed for {image_ref}: {result.stderr.strip()}")
        return False

    return _recreate_with_image(container_name, image_ref)


def recreate_with_image_id(container_name: str, image_id: str) -> bool:
    """Roll back a container to a specific image ID."""
    return _recreate_with_image(container_name, image_id)


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
    """
    List all containers using the low-level API to get full inspect data
    in a single call per container, with a short timeout to avoid hanging
    on a single unresponsive container.
    """
    result = []
    try:
        # Use the low-level API — returns a list of dicts with basic info
        # already included, avoiding the extra inspect call that .list() does.
        raw_list = client.api.containers(all=True, size=False)
    except Exception as e:
        logger.error(f"Failed to list containers: {e}")
        return []

    for raw in raw_list:
        try:
            # Inspect individually with a tight timeout
            container_id = raw["Id"]
            attrs = client.api.inspect_container(container_id)

            state = attrs.get("State", {})
            health = state.get("Health")
            image_info = attrs.get("Config", {}).get("Image", "")

            # Get image tags from the image object
            image_id = attrs.get("Image", "")
            image_tags = []
            try:
                img = client.images.get(image_id)
                image_tags = img.tags
            except Exception:
                pass

            # Read compose metadata from labels (set automatically by Docker Compose)
            labels = attrs.get("Config", {}).get("Labels") or {}
            compose_project = labels.get("com.docker.compose.project")
            compose_service = labels.get("com.docker.compose.service")

            result.append({
                "name": attrs["Name"].lstrip("/"),
                "status": state.get("Status", "unknown"),
                "image": image_tags[0] if image_tags else image_info,
                "image_id": image_id,
                "health": health.get("Status") if health else None,
                "started": state.get("StartedAt"),
                "compose_project": compose_project,
                "compose_service": compose_service,
            })
        except Exception as e:
            # One bad container shouldn't break the whole list
            name = raw.get("Names", ["unknown"])[0].lstrip("/")
            logger.warning(f"Could not inspect container {name}: {e}")
            result.append({
                "name": name,
                "status": "unknown",
                "image": raw.get("Image", "unknown"),
                "image_id": raw.get("ImageID", ""),
                "health": None,
                "started": None,
                "compose_project": None,
                "compose_service": None,
            })

    return result

# ─────────────────────────────────────────────
# Update check
# ─────────────────────────────────────────────

def check_for_update(image_tag: str) -> dict:
    """
    Check if a newer image is available without downloading image layers.

    Uses `docker pull --quiet` to fetch the latest manifest metadata only,
    then compares the image ID before and after. Docker updates the local
    image metadata on pull even if layers are already cached, so this is
    both fast (cached layers are not re-downloaded) and accurate for all
    registries including lscr.io and ghcr.io.

    Returns:
        up_to_date: True/False/None
        updated: True if a new image was pulled (caller may want to note this)
    """
    import subprocess

    try:
        # Capture image ID before pull
        try:
            local_image = client.images.get(image_tag)
            before_id = local_image.id
            before_digest = local_image.attrs.get("RepoDigests", [""])[0]
        except docker.errors.ImageNotFound:
            return {"up_to_date": None, "error": "Image not found locally"}

        # Pull latest manifest — layers already cached won't be re-downloaded
        pull_result = subprocess.run(
            ["docker", "pull", "--quiet", image_tag],
            capture_output=True, text=True, timeout=120
        )
        if pull_result.returncode != 0:
            return {"up_to_date": None, "error": f"Pull failed: {pull_result.stderr.strip()}"}

        # Compare image ID after pull
        try:
            after_image = client.images.get(image_tag)
            after_id = after_image.id
            after_digest = after_image.attrs.get("RepoDigests", [""])[0]
        except docker.errors.ImageNotFound:
            return {"up_to_date": None, "error": "Image not found after pull"}

        up_to_date = before_id == after_id

        return {
            "up_to_date": up_to_date,
            "local_digest": (after_digest.split("@")[-1])[:19] if "@" in after_digest else None,
        }

    except Exception as e:
        logger.error(f"Update check failed for {image_tag}: {e}")
        return {"up_to_date": None, "error": str(e)}