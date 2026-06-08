"""SQLAlchemy 引擎与会话。"""
from collections.abc import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()

_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=_connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app.db import models  # noqa: F401  注册所有表

    Base.metadata.create_all(bind=engine)
    _migrate_add_columns()


def _migrate_add_columns() -> None:
    """轻量迁移:为已存在的表补加新增列(create_all 不会改已存在的表)。"""
    wanted = {"servers": [("ssh_key_id", "INTEGER")]}
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, cols in wanted.items():
            if not insp.has_table(table):
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for col, coltype in cols:
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"))
