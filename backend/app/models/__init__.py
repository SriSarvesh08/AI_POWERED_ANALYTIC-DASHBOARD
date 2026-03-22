"""
SQLAlchemy ORM models
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column, String, DateTime, Text, Boolean,
    Integer, Float, ForeignKey, JSON, Enum
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.session import Base, AuthBase


# ── Auth models (separate DB) ────────────────────────────────────────────────

class User(AuthBase):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    full_name = Column(String(255))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


# ── Main app models ──────────────────────────────────────────────────────────

class Dataset(Base):
    __tablename__ = "datasets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    source_type = Column(
        Enum("csv", "excel", "sql_dump", "postgresql", "mysql", "snowflake", "firebase",
             name="source_type_enum"),
        nullable=False
    )
    # For uploads: original filename; for DB: connection alias
    source_reference = Column(Text)
    # Sanitised table name in our PostgreSQL data warehouse
    warehouse_table = Column(String(255))
    row_count = Column(Integer, default=0)
    column_count = Column(Integer, default=0)
    status = Column(
        Enum("pending", "ingested", "ai_analyzing", "ai_done", "superset_ready", "error",
             name="dataset_status_enum"),
        default="pending"
    )
    error_message = Column(Text)
    raw_schema = Column(JSON)        # Full schema before AI filtering
    ai_schema = Column(JSON)         # AI-selected columns + chart suggestions
    superset_dataset_id = Column(Integer)
    superset_dashboard_id = Column(Integer)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), onupdate=lambda: datetime.now(timezone.utc))

    charts = relationship("Chart", back_populates="dataset", cascade="all, delete-orphan")
    embeddings = relationship("ChunkEmbedding", back_populates="dataset", cascade="all, delete-orphan")


class Chart(Base):
    __tablename__ = "charts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dataset_id = Column(UUID(as_uuid=True), ForeignKey("datasets.id"), nullable=False)
    user_id = Column(UUID(as_uuid=True), nullable=False)
    superset_chart_id = Column(Integer)
    title = Column(String(255))
    chart_type = Column(String(100))   # bar, line, pie, scatter, …
    sql_query = Column(Text)
    ai_reasoning = Column(Text)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    dataset = relationship("Dataset", back_populates="charts")


class ChunkEmbedding(Base):
    """RAG store — one row per data chunk with pgvector embedding."""
    __tablename__ = "chunk_embeddings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dataset_id = Column(UUID(as_uuid=True), ForeignKey("datasets.id"), nullable=False)
    chunk_text = Column(Text, nullable=False)
    # Vector stored as JSON list until pgvector extension is confirmed available
    embedding = Column(JSON)
    chunk_index = Column(Integer)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    dataset = relationship("Dataset", back_populates="embeddings")


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dataset_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    step = Column(String(100))
    status = Column(Enum("running", "done", "error", name="run_status_enum"), default="running")
    detail = Column(Text)
    started_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    finished_at = Column(DateTime(timezone=True))
