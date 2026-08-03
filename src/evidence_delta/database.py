from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from evidence_delta.models import Base


class Database:
    def __init__(self, url: str) -> None:
        url = self.normalize_url(url)
        engine_options: dict = {"future": True}
        if url.startswith("sqlite"):
            engine_options["connect_args"] = {"check_same_thread": False}
            if ":memory:" in url:
                engine_options["poolclass"] = StaticPool

        else:
            engine_options["pool_pre_ping"] = True

        self.engine: Engine = create_engine(url, **engine_options)

        if url.startswith("sqlite"):
            event.listen(self.engine, "connect", self._enable_sqlite_foreign_keys)

        self._session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            expire_on_commit=False,
            future=True,
        )

    @staticmethod
    def normalize_url(url: str) -> str:
        """Use psycopg 3 for provider URLs that omit a SQLAlchemy driver."""

        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+psycopg://", 1)
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+psycopg://", 1)
        return url

    @staticmethod
    def _enable_sqlite_foreign_keys(connection, _record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    def ensure_schema(self) -> None:
        """Bring the database schema up to date for the running application."""

        if self.engine.dialect.name == "sqlite":
            self.create_schema()
            return
        self.migrate_schema()

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)
        if self.engine.dialect.name == "sqlite":
            self._upgrade_legacy_sqlite_schema()

    def migrate_schema(self) -> None:
        """Apply Alembic migrations for durable PostgreSQL deployments."""

        from alembic import command
        from alembic.config import Config

        alembic_ini = Path(__file__).resolve().parents[2] / "alembic.ini"
        config = Config(str(alembic_ini))
        database_url = os.environ.get("DATABASE_URL", str(self.engine.url))
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
        command.upgrade(config, "head")

    def _upgrade_legacy_sqlite_schema(self) -> None:
        """Keep local databases created before Alembic adoption usable."""

        with self.engine.begin() as connection:
            inspector = inspect(connection)
            case_columns = {column["name"] for column in inspector.get_columns("cases")}
            case_upgrades = {
                "assigned_officer": "VARCHAR(160)",
                "assigned_badge": "VARCHAR(80)",
                "assigned_unit": "VARCHAR(160)",
                "handoff_note": "TEXT",
            }
            for column, definition in case_upgrades.items():
                if column not in case_columns:
                    connection.execute(text(f"ALTER TABLE cases ADD COLUMN {column} {definition}"))

            document_columns = {column["name"] for column in inspector.get_columns("documents")}
            if "source_uri" not in document_columns:
                connection.execute(
                    text("ALTER TABLE documents ADD COLUMN source_uri VARCHAR(2048)")
                )

            assertion_columns = {column["name"] for column in inspector.get_columns("assertions")}
            if "time_precision" not in assertion_columns:
                connection.execute(
                    text(
                        "ALTER TABLE assertions ADD COLUMN time_precision "
                        "VARCHAR(16) NOT NULL DEFAULT 'EXACT'"
                    )
                )

            if "change_sets" in inspector.get_table_names():
                change_set_columns = {
                    column["name"] for column in inspector.get_columns("change_sets")
                }
                if "performed_by" not in change_set_columns:
                    connection.execute(
                        text("ALTER TABLE change_sets ADD COLUMN performed_by VARCHAR(160)")
                    )

    def drop_schema(self) -> None:
        Base.metadata.drop_all(self.engine)

    def ping(self) -> None:
        with self.engine.connect() as connection:
            connection.execute(select(1))

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
        finally:
            session.close()
