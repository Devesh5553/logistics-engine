import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Boolean, DateTime, ForeignKey, Text, Index
)
from sqlalchemy.orm import relationship
from src.db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(String(64), primary_key=True, default=lambda: f"usr_{uuid.uuid4().hex[:12]}")
    username = Column(String(64), unique=True, nullable=False, index=True)
    full_name = Column(String(128), nullable=False)
    email = Column(String(128), nullable=True)
    user_type = Column(String(32), nullable=False, default="staff")  # 'staff', 'client_user', 'service_account'
    client_id = Column(String(32), ForeignKey("clients.id", ondelete="SET NULL"), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    deactivated_at = Column(DateTime(timezone=True), nullable=True)

    # Absence tracking (Item 3)
    is_absent = Column(Boolean, default=False, nullable=False)
    absent_from = Column(DateTime(timezone=True), nullable=True)
    absent_to = Column(DateTime(timezone=True), nullable=True)
    absence_reason = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    client = relationship("Client", back_populates="users")
    grants = relationship("Grant", foreign_keys="[Grant.user_id]", back_populates="user")
    assigned_jobs = relationship("Assignment", back_populates="user")

    def __repr__(self):
        return f"<User {self.username} ({self.user_type}) active={self.is_active} absent={self.is_absent}>"


class Role(Base):
    __tablename__ = "roles"

    id = Column(String(32), primary_key=True)  # e.g. 'Maker', 'Checker', 'Admin', 'Manager', 'Auditor', 'Viewer', 'Parser'
    name = Column(String(64), unique=True, nullable=False)
    description = Column(Text, nullable=True)
    is_system = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    permissions = relationship("RolePermission", back_populates="role", cascade="all, delete-orphan")
    grants = relationship("Grant", back_populates="role")

    def __repr__(self):
        return f"<Role {self.name}>"


class Permission(Base):
    __tablename__ = "permissions"

    id = Column(String(64), primary_key=True)  # e.g. 'job:view', 'job:prepare', 'job:check'
    name = Column(String(64), nullable=False)
    category = Column(String(32), nullable=False)  # 'view', 'act', 'admin' - view permissions separate from act
    description = Column(Text, nullable=True)

    # Operational guardrails (Item 4 & Item 7)
    requires_reason = Column(Boolean, default=False, nullable=False)
    requires_second_approver = Column(Boolean, default=False, nullable=False)
    stage_limits = Column(String(128), nullable=True)  # e.g. "Confirmed, Prepared"

    # Relationships
    roles = relationship("RolePermission", back_populates="permission", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Permission {self.id} ({self.category}) approver={self.requires_second_approver}>"


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id = Column(String(32), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)
    permission_id = Column(String(64), ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True)

    role = relationship("Role", back_populates="permissions")
    permission = relationship("Permission", back_populates="roles")


class Grant(Base):
    """
    Scoped role-based access control with attribute constraints on each grant:
    client, branch, direction, mode (NULL means all/wildcard).
    Grants are never overwritten, only end-dated or revoked.
    """
    __tablename__ = "grants"

    id = Column(String(64), primary_key=True, default=lambda: f"grnt_{uuid.uuid4().hex[:12]}")
    user_id = Column(String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role_id = Column(String(32), ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False, index=True)

    # Attribute constraints (NULL = all / wildcard)
    client_id = Column(String(32), ForeignKey("clients.id", ondelete="SET NULL"), nullable=True)
    branch_id = Column(String(32), ForeignKey("branches.id", ondelete="SET NULL"), nullable=True)
    direction = Column(String(16), nullable=True)  # 'IMPORT', 'EXPORT', NULL
    mode = Column(String(16), nullable=True)       # 'AIR', 'FCL', 'LCL', NULL

    # Validity lifecycle
    valid_from = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    valid_to = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revocation_reason = Column(Text, nullable=True)

    # Governance & Dual Control
    granted_by = Column(String(64), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_by = Column(String(64), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    source = Column(String(32), default="direct", nullable=False)  # 'direct', 'delegation'
    parent_grant_id = Column(String(64), ForeignKey("grants.id", ondelete="SET NULL"), nullable=True)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    user = relationship("User", foreign_keys=[user_id], back_populates="grants")
    granter = relationship("User", foreign_keys=[granted_by])
    approver = relationship("User", foreign_keys=[approved_by])
    role = relationship("Role", back_populates="grants")
    client = relationship("Client", back_populates="grants")
    branch = relationship("Branch", back_populates="grants")
    parent_grant = relationship("Grant", remote_side=[id])

    __table_args__ = (
        Index("ix_grants_user_validity", "user_id", "valid_to", "revoked_at", "valid_from"),
        Index("ix_grants_scope", "user_id", "role_id", "client_id", "branch_id", "direction", "mode"),
    )

    def is_active_at(self, at: datetime) -> bool:
        def to_naive_utc(dt: datetime | None) -> datetime | None:
            if dt is None:
                return None
            if hasattr(dt, "tzinfo") and dt.tzinfo is not None:
                return dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt

        at_n = to_naive_utc(at)
        vf_n = to_naive_utc(self.valid_from)
        vt_n = to_naive_utc(self.valid_to)
        ra_n = to_naive_utc(self.revoked_at)

        if vf_n and at_n < vf_n:
            return False
        if vt_n and at_n > vt_n:
            return False
        if ra_n and at_n >= ra_n:
            return False
        return True

    def __repr__(self):
        return f"<Grant {self.id}: User={self.user_id} Role={self.role_id} Cl={self.client_id} Br={self.branch_id} Dir={self.direction} Mode={self.mode}>"
