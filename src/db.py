from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker
from src.config import DATABASE_URL

Base = declarative_base()


def get_engine(db_url: str = DATABASE_URL, echo: bool = False):
    connect_args = {}
    if db_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    engine = create_engine(db_url, echo=echo, connect_args=connect_args)

    if db_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.close()

    return engine


engine = get_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db(target_engine=None):
    """Create all tables in the specified or default database engine."""
    eng = target_engine or engine
    Base.metadata.create_all(bind=eng)


def drop_db(target_engine=None):
    """Drop all tables in the specified or default database engine."""
    eng = target_engine or engine
    Base.metadata.drop_all(bind=eng)
