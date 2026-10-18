import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_DIR = BASE_DIR / "data"
DB_DIR.mkdir(exist_ok=True)

DEFAULT_SQLITE_URL = f"sqlite:///{DB_DIR / 'access_control.db'}"
DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_SQLITE_URL)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

SCALE_DB_URL = f"sqlite:///{DB_DIR / 'access_control_scale.db'}"
