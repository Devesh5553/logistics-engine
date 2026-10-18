import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.db import Base
from src.seeds.sample_data import seed_sample_data


from sqlalchemy.pool import StaticPool


@pytest.fixture(scope="session")
def test_engine():
    """Session-scoped in-memory SQLite database with thread-safe StaticPool."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False
    )
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture(scope="function")
def db_session(test_engine):
    """Provides a transactional db session for each test."""
    connection = test_engine.connect()
    transaction = connection.begin()
    Session = sessionmaker(bind=connection)
    session = Session()

    # Seed sample data for the test
    seed_sample_data(session)

    yield session

    session.close()
    transaction.rollback()
    connection.close()
