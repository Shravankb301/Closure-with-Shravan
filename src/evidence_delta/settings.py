from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_DATABASE_URL = "sqlite:///./evidence_delta.db"
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _environment_flag(name: str, *, default: bool) -> bool:
    fallback = "true" if default else "false"
    return os.getenv(name, fallback).strip().casefold() in TRUE_VALUES


@dataclass(frozen=True)
class AppSettings:
    database_url: str
    demo_api_key: str | None
    demo_read_only_key: str | None
    public_demo_mode: bool
    embedded_worker: bool
    manual_drain: bool
    enable_local_ocr: bool
    artifact_vault_dir: str | None

    @classmethod
    def from_environment(cls, database_url: str | None = None) -> AppSettings:
        demo_api_key = os.getenv("DEMO_API_KEY") or None
        if _environment_flag("REQUIRE_DEMO_API_KEY", default=False) and demo_api_key is None:
            raise RuntimeError("DEMO_API_KEY is required for this deployment")
        return cls(
            database_url=database_url or os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL),
            demo_api_key=demo_api_key,
            demo_read_only_key=os.getenv("DEMO_READ_ONLY_KEY") or None,
            public_demo_mode=_environment_flag("PUBLIC_DEMO_MODE", default=False),
            embedded_worker=_environment_flag("RUN_EMBEDDED_WORKER", default=False),
            manual_drain=_environment_flag("ENABLE_MANUAL_DRAIN", default=True),
            enable_local_ocr=_environment_flag("ENABLE_LOCAL_OCR", default=True),
            artifact_vault_dir=os.getenv(
                "ARTIFACT_VAULT_DIR", "./.evidence_delta_artifacts"
            ).strip()
            or None,
        )
