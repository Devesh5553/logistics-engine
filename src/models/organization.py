from datetime import datetime, timezone
from sqlalchemy import Column, String, Boolean, DateTime
from sqlalchemy.orm import relationship
from src.db import Base


class Branch(Base):
    __tablename__ = "branches"

    id = Column(String(32), primary_key=True)  # e.g. 'DEL', 'MUM', 'KOL', 'MAA', 'BLR'
    name = Column(String(128), nullable=False)
    city = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    jobs = relationship("Job", back_populates="branch")
    grants = relationship("Grant", back_populates="branch")

    def __repr__(self):
        return f"<Branch {self.id}: {self.name}>"


class Client(Base):
    __tablename__ = "clients"

    id = Column(String(32), primary_key=True)  # e.g. 'CL-001'
    name = Column(String(128), nullable=False)
    industry = Column(String(64), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    jobs = relationship("Job", back_populates="client")
    users = relationship("User", back_populates="client")
    grants = relationship("Grant", back_populates="client")

    def __repr__(self):
        return f"<Client {self.id}: {self.name}>"
