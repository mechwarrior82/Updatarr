import urllib.request
import json
import re
import logging
from datetime import datetime
from sqlalchemy.orm import Session
from db import ContainerConfig

logger = logging.getLogger(__name__)

# Tags to skip when scanning Docker Hub
_NOISE_TAGS = re.compile(
    r'^(latest|main|master|develop|nightly|unstable|edge|beta|alpha|preview'
    r'|sha-[0-9a-f]+'
    r'|arm64v8-.*|amd64-.*'
    r'|.*-develop$|.*-nightly$'
    r')$',
    re.IGNORECASE
)


# ─────────────────────────────────────────────
# HTTP helper
# ─────────────────────────────────────────────

def _get(url: str) -> tuple[int, dict | list | None]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "updatarr"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        logger.debug(f"Request failed for {url}: {e}")
        return 0, None


# ─────────────────────────────────────────────
# Docker Hub
# ─────────────────────────────────────────────

def _guess_dockerhub_repo(image_tag: str) -> str | None:
    """
    Derive the Docker Hub repo slug from an image tag.
      lscr.io/linuxserver/sonarr  -> linuxserver/sonarr
      ghcr.io/recyclarr/recyclarr -> recyclarr/recyclarr  (may not exist but worth trying)
      fallenbagel/jellyseerr      -> fallenbagel/jellyseerr
      golift/unpackerr            -> golift/unpackerr
    """
    if not image_tag:
        return None
    image = image_tag.split(":")[0]
    parts = image.split("/")

    if len(parts) == 3:
        _, org, name = parts
        return f"{org}/{name}"
    if len(parts) == 2:
        return f"{parts[0]}/{parts[1]}"
    return None


def _dockerhub_latest(repo: str) -> str | None:
    """Return the most recently updated non-noise tag from Docker Hub."""
    status, data = _get(
        f"https://hub.docker.com/v2/repositories/{repo}/tags"
        f"?page_size=20&ordering=last_updated"
    )
    if status != 200 or not data:
        return None
    for tag in data.get("results", []):
        name = tag.get("name", "")
        if not _NOISE_TAGS.match(name):
            return name
    return None


# ─────────────────────────────────────────────
# GitHub
# ─────────────────────────────────────────────

def _guess_github_repo(image_tag: str) -> str | None:
    """
    Guess the GitHub repo from an image tag — used only to pre-fill the UI.
      lscr.io/linuxserver/sonarr  -> linuxserver/docker-sonarr
      ghcr.io/recyclarr/recyclarr -> recyclarr/recyclarr
      fallenbagel/jellyseerr      -> fallenbagel/jellyseerr
    """
    if not image_tag:
        return None
    image = image_tag.split(":")[0]
    parts = image.split("/")

    if len(parts) == 3:
        registry, org, name = parts
        if registry == "lscr.io" and org == "linuxserver":
            return f"linuxserver/docker-{name}"
        return f"{org}/{name}"
    if len(parts) == 2:
        return f"{parts[0]}/{parts[1]}"
    return None


def _github_latest(repo: str) -> tuple[str | None, str]:
    """Try GitHub releases then tags. Returns (version, endpoint_used)."""
    status, data = _get(f"https://api.github.com/repos/{repo}/releases/latest")
    if status == 200 and data and data.get("tag_name"):
        return data["tag_name"], "github_releases"

    status, data = _get(f"https://api.github.com/repos/{repo}/tags")
    if status == 200 and data and len(data) > 0:
        return data[0].get("name"), "github_tags"

    return None, "github_releases"


# ─────────────────────────────────────────────
# Core check logic
# ─────────────────────────────────────────────

def check_version(container_name: str, db: Session, image_tag: str = None) -> dict:
    """
    Check for the latest version of a container.

    Source priority:
      1. GitHub (if github_repo is explicitly set by the user)
      2. Docker Hub (default, auto-detected from image tag)
    """
    cfg = db.query(ContainerConfig).filter_by(name=container_name).first()
    if not cfg:
        cfg = ContainerConfig(name=container_name)
        db.add(cfg)
        db.commit()

    # ── GitHub path (user explicitly configured) ──────────────────
    if cfg.github_repo:
        latest, endpoint = _github_latest(cfg.github_repo)
        if latest:
            cfg.github_endpoint = endpoint
            cfg.latest_version = latest
            cfg.version_checked_at = datetime.utcnow()
            cfg.update_available = (
                cfg.current_version is not None and cfg.current_version != latest
            )
            db.commit()
            return {
                "status": "ok",
                "source": endpoint,
                "repo": cfg.github_repo,
                "current_version": cfg.current_version,
                "latest_version": latest,
                "update_available": cfg.update_available,
                "github_suggestion": None,
                "checked_at": cfg.version_checked_at.isoformat(),
            }
        # GitHub repo set but unreachable — fall through to Docker Hub
        logger.warning(f"[{container_name}] GitHub repo {cfg.github_repo} unreachable, falling back to Docker Hub")

    # ── Docker Hub path (default) ─────────────────────────────────
    dh_repo = _guess_dockerhub_repo(image_tag) if image_tag else None

    if dh_repo:
        latest = _dockerhub_latest(dh_repo)
        if latest:
            cfg.dockerhub_repo = dh_repo
            cfg.github_endpoint = "dockerhub"
            cfg.latest_version = latest
            cfg.version_checked_at = datetime.utcnow()
            cfg.update_available = (
                cfg.current_version is not None and cfg.current_version != latest
            )
            db.commit()

            # Pre-fill GitHub suggestion for the UI without saving it
            gh_suggestion = _guess_github_repo(image_tag) if image_tag else None

            return {
                "status": "ok",
                "source": "dockerhub",
                "repo": dh_repo,
                "current_version": cfg.current_version,
                "latest_version": latest,
                "update_available": cfg.update_available,
                "github_suggestion": gh_suggestion,
                "checked_at": cfg.version_checked_at.isoformat(),
            }

    cfg.github_endpoint = "unsupported"
    db.commit()
    return {
        "status": "unsupported",
        "detail": "Could not find this image on Docker Hub or GitHub.",
        "github_suggestion": _guess_github_repo(image_tag) if image_tag else None,
    }


def record_current_version(container_name: str, db: Session):
    """Record the latest version as current after a successful update."""
    cfg = db.query(ContainerConfig).filter_by(name=container_name).first()
    if not cfg or cfg.github_endpoint == "unsupported":
        return

    latest = None
    if cfg.github_repo and cfg.github_endpoint in ("github_releases", "github_tags"):
        latest, _ = _github_latest(cfg.github_repo)
    elif cfg.github_endpoint == "dockerhub" and cfg.dockerhub_repo:
        latest = _dockerhub_latest(cfg.dockerhub_repo)

    if latest:
        cfg.current_version = latest
        cfg.latest_version = latest
        cfg.update_available = False
        cfg.version_checked_at = datetime.utcnow()
        db.commit()
        logger.info(f"[{container_name}] Recorded version: {latest} (via {cfg.github_endpoint})")


def check_all_versions(db: Session) -> list[dict]:
    """Check versions for all monitored containers."""
    import docker as docker_sdk
    client = docker_sdk.from_env()

    monitored = db.query(ContainerConfig).filter_by(monitored=True).all()
    results = []

    for cfg in monitored:
        image_tag = None
        try:
            c = client.containers.get(cfg.name)
            image_tag = c.image.tags[0] if c.image.tags else None
        except Exception:
            pass

        result = check_version(cfg.name, db, image_tag=image_tag)
        results.append({"container": cfg.name, **result})

    return results
