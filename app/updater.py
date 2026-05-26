import logging
from datetime import datetime
from sqlalchemy.orm import Session

from docker_ops import (
    get_current_image_id,
    pull_latest_image,
    backup_volumes,
    restore_volumes,
    recreate_with_new_image,
    recreate_with_image_id,
    wait_for_health,
)
from db import ContainerConfig, UpdateEvent, BackupRecord

logger = logging.getLogger(__name__)


def _tag_now() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")


def run_update(container_name: str, db: Session) -> dict:
    """
    Full update flow for a single container:
      1. Check for hold
      2. Backup volumes
      3. Pull new image
      4. Recreate container
      5. Verify health
      6. On failure: restore backup, rollback image, set hold
    """
    # ── 1. Check hold ────────────────────────────────────────────
    cfg = db.query(ContainerConfig).filter_by(name=container_name).first()
    if not cfg:
        return {"status": "skipped", "reason": "Container not in monitored list"}
    if cfg.held:
        return {"status": "skipped", "reason": f"Container is held: {cfg.hold_reason}"}
    if not cfg.monitored:
        return {"status": "skipped", "reason": "Container not monitored"}

    tag = _tag_now()
    event = UpdateEvent(
        container_name=container_name,
        status="pending",
        backup_tag=tag,
        started_at=datetime.utcnow(),
    )
    db.add(event)
    db.commit()

    def _fail(reason: str):
        event.status = "failed"
        event.detail = reason
        event.finished_at = datetime.utcnow()
        db.commit()
        return {"status": "failed", "reason": reason}

    def _success(detail: str):
        event.status = "success"
        event.detail = detail
        event.finished_at = datetime.utcnow()
        db.commit()
        return {"status": "success", "detail": detail}

    # ── 2. Capture current image ID for potential rollback ────────
    old_image_id = get_current_image_id(container_name)
    event.old_image_id = old_image_id
    db.commit()

    # ── 3. Backup volumes ─────────────────────────────────────────
    logger.info(f"[{container_name}] Starting pre-update backup (tag={tag})")
    backed_up_files = backup_volumes(container_name, tag, stop_first=True)

    backup_record = BackupRecord(
        container_name=container_name,
        tag=tag,
        trigger="pre_update",
        files=backed_up_files,
    )
    db.add(backup_record)
    db.commit()

    # ── 4. Pull + recreate ────────────────────────────────────────
    logger.info(f"[{container_name}] Pulling latest image and recreating")
    success = recreate_with_new_image(container_name, "")
    if not success:
        return _fail("Failed to recreate container with new image")

    new_image_id = get_current_image_id(container_name)
    event.new_image_id = new_image_id
    db.commit()

    if old_image_id == new_image_id:
        event.status = "success"
        event.detail = "Already up to date"
        event.finished_at = datetime.utcnow()
        db.commit()
        return {"status": "success", "detail": "Already up to date"}

    # ── 5. Health check ───────────────────────────────────────────
    timeout = cfg.health_timeout or 90
    healthy = wait_for_health(container_name, timeout=timeout)

    if healthy:
        return _success(f"Updated successfully. New image: {new_image_id[:12] if new_image_id else 'unknown'}")

    # ── 6. Rollback ───────────────────────────────────────────────
    logger.error(f"[{container_name}] Health check failed — initiating rollback")

    restore_ok = True
    if backed_up_files:
        restore_ok = restore_volumes(container_name, tag)

    rollback_ok = False
    if old_image_id:
        rollback_ok = recreate_with_image_id(container_name, old_image_id)

    hold_reason = (
        f"Auto-rolled back after failed update on {tag}. "
        f"Volume restore: {'OK' if restore_ok else 'FAILED'}. "
        f"Image rollback: {'OK' if rollback_ok else 'FAILED'}."
    )

    cfg.held = True
    cfg.hold_reason = hold_reason
    event.status = "rolled_back"
    event.detail = hold_reason
    event.finished_at = datetime.utcnow()
    db.commit()

    logger.error(f"[{container_name}] Rollback complete. Container placed on hold.")
    return {"status": "rolled_back", "reason": hold_reason}


def run_all_updates(db: Session) -> list[dict]:
    """Update all monitored, non-held containers one at a time."""
    monitored = db.query(ContainerConfig).filter_by(monitored=True, held=False).all()
    results = []

    # Always do gluetun last — it takes the whole VPN stack with it
    ordered = sorted(monitored, key=lambda c: 1 if c.name == "gluetun" else 0)

    for cfg in ordered:
        logger.info(f"Processing update for: {cfg.name}")
        result = run_update(cfg.name, db)
        results.append({"container": cfg.name, **result})

    return results


def run_manual_backup(container_name: str, db: Session) -> dict:
    tag = _tag_now()
    files = backup_volumes(container_name, tag)
    if not files:
        return {"status": "warning", "detail": "No named volumes found or backup failed"}

    record = BackupRecord(
        container_name=container_name,
        tag=tag,
        trigger="manual",
        files=files,
    )
    db.add(record)
    db.commit()
    return {"status": "success", "tag": tag, "files": files}


def run_manual_restore(container_name: str, tag: str, db: Session) -> dict:
    ok = restore_volumes(container_name, tag)
    if not ok:
        return {"status": "failed", "detail": f"Restore failed for tag {tag}"}
    return {"status": "success", "detail": f"Restored {container_name} from tag {tag}"}


def release_hold(container_name: str, db: Session) -> dict:
    cfg = db.query(ContainerConfig).filter_by(name=container_name).first()
    if not cfg:
        return {"status": "not_found"}
    cfg.held = False
    cfg.hold_reason = None
    db.commit()
    return {"status": "success", "detail": f"Hold released for {container_name}"}
