"""Persistent task and authorization records, supported on SQLite and PostgreSQL."""

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime) -> str:
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)).isoformat()


class Base(DeclarativeBase):
    pass


class Identity(Base):
    __tablename__ = "identities"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[str] = mapped_column(String(20))
    classroom_ids: Mapped[list] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TenantQuota(Base):
    __tablename__ = "tenant_quotas"
    tenant_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "owner_id", "idempotency_key"),
        Index("ix_jobs_queue", "status", "created_at"),
        Index("ix_jobs_classroom", "tenant_id", "classroom_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80))
    classroom_id: Mapped[str] = mapped_column(String(80))
    owner_id: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    text: Mapped[str] = mapped_column(Text, default="")
    text_hash: Mapped[str] = mapped_column(String(64), default="")
    text_version: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(40), default="needs_text_review")
    image_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    ocr: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    rubric: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    review: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class JobEvent(Base):
    __tablename__ = "job_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), index=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    actor_id: Mapped[str] = mapped_column(String(64))
    event: Mapped[str] = mapped_column(String(64))
    job_version: Mapped[int] = mapped_column(Integer)
    # Never store student text or provider responses in audit events.
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class WritingRevision(Base):
    """Independent manual revision; original approved Job content stays immutable."""
    __tablename__ = "writing_revisions"
    __table_args__ = (
        UniqueConstraint("job_id", "ordinal"),
        UniqueConstraint("job_id", "owner_id", "idempotency_key"),
        Index("ix_writing_revisions_scope", "tenant_id", "job_id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80))
    classroom_id: Mapped[str] = mapped_column(String(80))
    job_id: Mapped[str] = mapped_column(String(36))
    owner_id: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(100))
    ordinal: Mapped[int] = mapped_column(Integer)
    source_binding: Mapped[dict] = mapped_column(JSON)
    text: Mapped[str] = mapped_column(Text)
    resolutions: Mapped[dict] = mapped_column(JSON, default=dict)
    note: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="draft")
    version: Mapped[int] = mapped_column(Integer, default=1)
    confirmed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RevisionAssessment(Base):
    """One version-bound independent Job per confirmed manual revision."""
    __tablename__ = "revision_assessments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "revision_id"),
        UniqueConstraint("tenant_id", "requested_by", "idempotency_key"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    classroom_id: Mapped[str] = mapped_column(String(80))
    source_job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("writing_revisions.id", ondelete="CASCADE"))
    revision_version: Mapped[int] = mapped_column(Integer)
    child_job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), unique=True)
    requested_by: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(100))
    binding: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Candidate(Base):
    __tablename__ = "candidates"
    __table_args__ = (UniqueConstraint("tenant_id", "job_id", "report_version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    classroom_id: Mapped[str] = mapped_column(String(80))
    job_id: Mapped[str] = mapped_column(String(36))
    report_version: Mapped[int] = mapped_column(Integer)
    submitted_by: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    provenance: Mapped[str] = mapped_column(Text)
    rubric: Mapped[dict] = mapped_column(JSON)
    review: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    version: Mapped[int] = mapped_column(Integer, default=1)
    reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CallReceipt(Base):
    __tablename__ = "call_receipts"
    __table_args__ = (UniqueConstraint("job_id", "input_hash", "stage"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80))
    job_id: Mapped[str] = mapped_column(String(36), index=True)
    input_hash: Mapped[str] = mapped_column(String(64))
    stage: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    resolutions: Mapped[list] = mapped_column(JSON, default=list)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


def create_session_factory(database_url: str):
    """Create connections without initializing or migrating a production schema."""
    kwargs = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 10}
        if database_url == "sqlite://":
            from sqlalchemy.pool import StaticPool
            kwargs["poolclass"] = StaticPool
    engine = create_engine(database_url, **kwargs)
    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def sqlite_pragmas(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


class ChunkTask(Base):
    __tablename__ = "chunk_tasks"
    __table_args__ = (UniqueConstraint("job_id","manifest_hash","ordinal"),)
    id: Mapped[str] = mapped_column(String(36),primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80))
    job_id: Mapped[str] = mapped_column(String(36),index=True)
    manifest_hash: Mapped[str] = mapped_column(String(64))
    ordinal: Mapped[int] = mapped_column(Integer)
    start: Mapped[int] = mapped_column(Integer)
    end: Mapped[int] = mapped_column(Integer)
    context_start: Mapped[int] = mapped_column(Integer)
    context_end: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20),default="queued")
    lease_id: Mapped[str | None] = mapped_column(String(36),nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON,nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64),nullable=True)


class Outbox(Base):
    __tablename__ = "outbox"
    id: Mapped[str] = mapped_column(String(36),primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36),index=True)
    chunk_id: Mapped[str] = mapped_column(String(36),unique=True)
    delivered: Mapped[bool] = mapped_column(Boolean,default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),default=now)


def initialize_database(database_url: str) -> None:
    """Explicit baseline initialization, invoked only by an operator or tests."""
    if database_url.startswith("sqlite:///"):
        path = Path(database_url.removeprefix("sqlite:///"))
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    engine, _ = create_session_factory(database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
