from sqlalchemy import create_engine, Column, String, Boolean, DateTime, JSON, Integer, Text
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
import os

DB_PATH = os.environ.get("DB_PATH", "/app/config/manager.db")
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


class ContainerConfig(Base):
    """Per-container monitoring config and hold state."""
    __tablename__ = "container_config"

    name = Column(String, primary_key=True)
    monitored = Column(Boolean, default=False)
    held = Column(Boolean, default=False)
    hold_reason = Column(Text, nullable=True)
    health_timeout = Column(Integer, default=90)
    notes = Column(Text, nullable=True)

    # Version tracking
    github_repo = Column(String, nullable=True)       # user-set GitHub repo e.g. "linuxserver/docker-sonarr"
    dockerhub_repo = Column(String, nullable=True)    # auto-detected Docker Hub slug e.g. "linuxserver/sonarr"
    github_endpoint = Column(String, nullable=True)   # active source: github_releases|github_tags|dockerhub|unsupported
    current_version = Column(String, nullable=True)   # version recorded at last update
    latest_version = Column(String, nullable=True)    # latest seen from source
    version_checked_at = Column(DateTime, nullable=True)
    update_available = Column(Boolean, nullable=True)

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class UpdateEvent(Base):
    """Audit log for every update attempt."""
    __tablename__ = "update_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    container_name = Column(String, nullable=False)
    old_image_id = Column(String, nullable=True)
    new_image_id = Column(String, nullable=True)
    backup_tag = Column(String, nullable=True)
    status = Column(String, nullable=False)
    detail = Column(Text, nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)


class BackupRecord(Base):
    """Track manual and automatic backups."""
    __tablename__ = "backup_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    container_name = Column(String, nullable=False)
    tag = Column(String, nullable=False)
    trigger = Column(String, default="manual")
    files = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
