import hashlib
import json
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, Index
from sqlalchemy.orm import relationship
from src.db import Base


class AuditLog(Base):
    """
    Append-only, hash-chained audit log.
    Records grant changes, decisions, denials, overrides, stage transitions,
    with grant IDs used and the effective permission snapshot.
    """
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False, index=True)
    event_type = Column(String(64), nullable=False)  # 'GRANT_CHANGE', 'DECISION', 'DENIAL', 'OVERRIDE', 'STAGE_TRANSITION'
    actor_id = Column(String(64), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    target_type = Column(String(64), nullable=False)  # 'job', 'grant', 'user', 'system'
    target_id = Column(String(64), nullable=True, index=True)
    action = Column(String(64), nullable=False)
    decision = Column(String(16), nullable=False)  # 'ALLOW', 'DENY', 'EXECUTED'
    deny_code = Column(String(64), nullable=True)
    reasons = Column(Text, nullable=True)  # JSON-encoded list or text
    grant_ids_used = Column(Text, nullable=True)  # JSON-encoded list of grant IDs
    permission_snapshot = Column(Text, nullable=True)  # JSON-encoded snapshot of permissions
    details = Column(Text, nullable=True)  # JSON payload of additional context

    # Hash chaining
    prev_hash = Column(String(64), nullable=False)
    record_hash = Column(String(64), nullable=False, index=True)

    # Relationship
    actor = relationship("User")

    __table_args__ = (
        Index("ix_audit_target", "target_type", "target_id"),
        Index("ix_audit_event_time", "event_type", "timestamp"),
    )

    @staticmethod
    def normalize_timestamp(ts: datetime | str | None) -> str:
        if ts is None:
            return ""
        if isinstance(ts, str):
            return ts
        if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
            ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
        return ts.strftime("%Y-%m-%d %H:%M:%S")

    @classmethod
    def compute_hash(
        cls,
        prev_hash: str,
        timestamp: datetime | str | None,
        event_type: str,
        actor_id: str | None,
        target_type: str,
        target_id: str | None,
        action: str,
        decision: str,
        deny_code: str | None,
        reasons: str | None,
        grant_ids_used: str | None,
        permission_snapshot: str | None,
        details: str | None
    ) -> str:
        timestamp_str = cls.normalize_timestamp(timestamp)
        payload = {
            "prev_hash": prev_hash,
            "timestamp": timestamp_str,
            "event_type": event_type,
            "actor_id": actor_id or "",
            "target_type": target_type,
            "target_id": target_id or "",
            "action": action,
            "decision": decision,
            "deny_code": deny_code or "",
            "reasons": reasons or "",
            "grant_ids_used": grant_ids_used or "",
            "permission_snapshot": permission_snapshot or "",
            "details": details or ""
        }
        encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def verify_integrity(self) -> bool:
        expected = self.compute_hash(
            prev_hash=self.prev_hash,
            timestamp=self.timestamp,
            event_type=self.event_type,
            actor_id=self.actor_id,
            target_type=self.target_type,
            target_id=self.target_id,
            action=self.action,
            decision=self.decision,
            deny_code=self.deny_code,
            reasons=self.reasons,
            grant_ids_used=self.grant_ids_used,
            permission_snapshot=self.permission_snapshot,
            details=self.details
        )
        return self.record_hash == expected

    def __repr__(self):
        return f"<AuditLog #{self.id}: {self.event_type} {self.action} {self.decision} hash={self.record_hash[:8]}>"

