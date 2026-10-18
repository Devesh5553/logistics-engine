import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from src.db import Base


class Approval(Base):
    """
    Second-person sign-off for high-risk actions (cancelling a filed job, reopening
    a closed job, admin override, admin grant, editing job scope).
    Single-use, time-limited.
    """
    __tablename__ = "approvals"

    id = Column(String(64), primary_key=True, default=lambda: f"appr_{uuid.uuid4().hex[:12]}")
    action = Column(String(64), nullable=True, index=True)
    target_type = Column(String(32), nullable=False)  # 'job', 'grant', 'user', or legacy 'JOB_CANCEL_FILED'
    target_id = Column(String(64), nullable=False, index=True)

    requested_by = Column(String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    approved_by = Column(String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=True)

    reason = Column(Text, nullable=False)
    status = Column(String(32), default="pending", nullable=False)  # 'pending', 'approved', 'rejected', 'used', 'expired'
    approved_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    requester = relationship("User", foreign_keys=[requested_by])
    approver = relationship("User", foreign_keys=[approved_by])

    def is_valid_at(self, at: datetime) -> bool:
        if not self.is_active:
            return False
        if self.status not in ("approved", "pending"):
            return False
        if self.approved_by is None or self.status == "pending":
            return False
        if self.used_at is not None:
            return False

        def to_naive(dt):
            if dt is None:
                return None
            if hasattr(dt, "tzinfo") and dt.tzinfo is not None:
                return dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt

        at_n = to_naive(at)
        exp_n = to_naive(self.expires_at)
        if exp_n and at_n and at_n > exp_n:
            return False
        return True

    def __repr__(self):
        return f"<Approval {self.id}: action={self.action} target={self.target_id} status={self.status}>"

