import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Depends, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db import init_db, get_db, ContainerConfig, UpdateEvent, BackupRecord
from docker_ops import list_all_containers, list_backups
from updater import (
    run_update,
    run_all_updates,
    run_manual_backup,
    run_manual_restore,
    release_hold,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

UPDATE_SCHEDULE = os.environ.get("UPDATE_SCHEDULE", "0 4 * * *")  # 4AM daily cron


# ─────────────────────────────────────────────
# Scheduler
# ─────────────────────────────────────────────

scheduler = BackgroundScheduler()


def scheduled_update_job():
    from db import SessionLocal
    db = SessionLocal()
    try:
        logger.info("Scheduled update run starting")
        results = run_all_updates(db)
        logger.info(f"Scheduled update complete: {results}")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Parse cron schedule from env
    parts = UPDATE_SCHEDULE.split()
    if len(parts) == 5:
        scheduler.add_job(
            scheduled_update_job,
            "cron",
            minute=parts[0], hour=parts[1],
            day=parts[2], month=parts[3], day_of_week=parts[4],
            id="scheduled_update",
        )
    scheduler.start()
    logger.info(f"Scheduler started. Update schedule: {UPDATE_SCHEDULE}")
    yield
    scheduler.shutdown()


app = FastAPI(title="Updatarr", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="/app/ui"), name="static")


@app.get("/")
def index():
    return FileResponse("/app/ui/index.html")


# ─────────────────────────────────────────────
# Container status
# ─────────────────────────────────────────────

@app.get("/api/containers")
def get_containers(db: Session = Depends(get_db)):
    """Return all Docker containers merged with their manager config."""
    docker_containers = list_all_containers()
    configs = {c.name: c for c in db.query(ContainerConfig).all()}

    result = []
    for c in docker_containers:
        cfg = configs.get(c["name"])
        result.append({
            **c,
            "monitored": cfg.monitored if cfg else False,
            "held": cfg.held if cfg else False,
            "hold_reason": cfg.hold_reason if cfg else None,
            "health_timeout": cfg.health_timeout if cfg else 90,
            "notes": cfg.notes if cfg else None,
        })
    return result


# ─────────────────────────────────────────────
# Container config (monitor/hold toggles)
# ─────────────────────────────────────────────

class ContainerConfigUpdate(BaseModel):
    monitored: bool | None = None
    held: bool | None = None
    hold_reason: str | None = None
    health_timeout: int | None = None
    notes: str | None = None


@app.post("/api/containers/{name}/config")
def update_container_config(name: str, body: ContainerConfigUpdate, db: Session = Depends(get_db)):
    cfg = db.query(ContainerConfig).filter_by(name=name).first()
    if not cfg:
        cfg = ContainerConfig(name=name)
        db.add(cfg)

    if body.monitored is not None:
        cfg.monitored = body.monitored
    if body.held is not None:
        cfg.held = body.held
    if body.hold_reason is not None:
        cfg.hold_reason = body.hold_reason
    if body.health_timeout is not None:
        cfg.health_timeout = body.health_timeout
    if body.notes is not None:
        cfg.notes = body.notes

    cfg.updated_at = datetime.utcnow()
    db.commit()
    return {"status": "ok", "name": name}


@app.post("/api/containers/{name}/release-hold")
def api_release_hold(name: str, db: Session = Depends(get_db)):
    return release_hold(name, db)


# ─────────────────────────────────────────────
# Updates
# ─────────────────────────────────────────────

@app.post("/api/update/{name}")
def api_update_one(name: str, db: Session = Depends(get_db)):
    return run_update(name, db)


@app.post("/api/update-all")
def api_update_all(db: Session = Depends(get_db)):
    return run_all_updates(db)


# ─────────────────────────────────────────────
# Backups
# ─────────────────────────────────────────────

@app.post("/api/backup/{name}")
def api_backup(name: str, db: Session = Depends(get_db)):
    return run_manual_backup(name, db)


@app.get("/api/backups/{name}")
def api_list_backups(name: str):
    return list_backups(name)


class RestoreRequest(BaseModel):
    tag: str


@app.post("/api/restore/{name}")
def api_restore(name: str, body: RestoreRequest, db: Session = Depends(get_db)):
    return run_manual_restore(name, body.tag, db)


# ─────────────────────────────────────────────
# Audit log
# ─────────────────────────────────────────────

@app.get("/api/events")
def api_events(limit: int = 50, db: Session = Depends(get_db)):
    events = (
        db.query(UpdateEvent)
        .order_by(UpdateEvent.started_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": e.id,
            "container": e.container_name,
            "status": e.status,
            "detail": e.detail,
            "backup_tag": e.backup_tag,
            "old_image": e.old_image_id[:12] if e.old_image_id else None,
            "new_image": e.new_image_id[:12] if e.new_image_id else None,
            "started_at": e.started_at.isoformat() if e.started_at else None,
            "finished_at": e.finished_at.isoformat() if e.finished_at else None,
        }
        for e in events
    ]


@app.get("/api/schedule")
def api_schedule():
    jobs = scheduler.get_jobs()
    return [
        {
            "id": j.id,
            "next_run": j.next_run_time.isoformat() if j.next_run_time else None,
        }
        for j in jobs
    ]
