import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Boolean, Integer, DateTime, ForeignKey, Text, Index
)
from sqlalchemy.orm import relationship
from src.db import Base


class Job(Base):
    __tablename__ = "jobs"

    id = Column(String(64), primary_key=True)  # e.g. 'JOB-0001'
    client_id = Column(String(32), ForeignKey("clients.id", ondelete="RESTRICT"), nullable=False, index=True)
    branch_id = Column(String(32), ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False, index=True)
    direction = Column(String(16), nullable=False)  # 'IMPORT', 'EXPORT'
    mode = Column(String(16), nullable=False)       # 'AIR', 'FCL', 'LCL'
    stage = Column(String(32), nullable=False, default="Confirmed")
    # Stages: 'Confirmed', 'Prepared', 'Checked', 'Filed', 'Cancelled', 'Reopened'

    title = Column(String(255), nullable=True)
    goods_description = Column(Text, nullable=True)

    prepared_by = Column(String(64), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    checked_by = Column(String(64), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # Optimistic locking & state protection
    version = Column(Integer, default=1, nullable=False)
    locked = Column(Boolean, default=False, nullable=False)
    locked_reason = Column(Text, nullable=True)

    # Test case note for deliberate planted jobs (Item 5)
    test_case_note = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    client = relationship("Client", back_populates="jobs")
    branch = relationship("Branch", back_populates="jobs")
    preparer = relationship("User", foreign_keys=[prepared_by])
    checker = relationship("User", foreign_keys=[checked_by])
    events = relationship("JobEvent", back_populates="job", cascade="all, delete-orphan", order_by="JobEvent.timestamp.asc()")
    assignments = relationship("Assignment", back_populates="job", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_jobs_scope_stage", "client_id", "branch_id", "direction", "mode", "stage"),
        Index("ix_jobs_branch_stage", "branch_id", "stage"),
        Index("ix_jobs_stage", "stage"),
    )

    def __repr__(self):
        return f"<Job {self.id}: {self.stage} [{self.client_id}|{self.branch_id}|{self.direction}|{self.mode}]>"


class JobEvent(Base):
    __tablename__ = "job_events"

    id = Column(String(64), primary_key=True, default=lambda: f"ev_{uuid.uuid4().hex[:12]}")
    job_id = Column(String(64), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    actor_id = Column(String(64), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    action = Column(String(64), nullable=False)  # 'CREATE', 'PREPARE', 'CHECK', 'FILE', 'CANCEL', 'REOPEN', 'EDIT_SCOPE'
    from_stage = Column(String(32), nullable=True)
    to_stage = Column(String(32), nullable=False)
    reason = Column(Text, nullable=True)
    old_values = Column(Text, nullable=True)  # JSON-serialized snapshot of previous values
    new_values = Column(Text, nullable=True)  # JSON-serialized snapshot of new values
    timestamp = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False, index=True)

    # Relationships
    job = relationship("Job", back_populates="events")
    actor = relationship("User")

    def __repr__(self):
        return f"<JobEvent {self.id}: {self.action} on {self.job_id} by {self.actor_id}>"


class Assignment(Base):
    __tablename__ = "assignments"

    id = Column(String(64), primary_key=True, default=lambda: f"asgn_{uuid.uuid4().hex[:12]}")
    job_id = Column(String(64), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    assigned_role = Column(String(32), nullable=False)  # 'Maker', 'Checker'
    is_active = Column(Boolean, default=True, nullable=False)
    needs_reassignment = Column(Boolean, default=False, nullable=False)  # Flagged when user is absent/revoked
    reassignment_reason = Column(Text, nullable=True)
    assigned_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    job = relationship("Job", back_populates="assignments")
    user = relationship("User", back_populates="assigned_jobs")

    def __repr__(self):
        return f"<Assignment {self.id}: Job={self.job_id} User={self.user_id} Role={self.assigned_role} Reassign={self.needs_reassignment}>"
