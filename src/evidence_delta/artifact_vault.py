from __future__ import annotations

import os
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True)
class VaultReceipt:
    uri: str
    sha256: str
    bytes_stored: int
    created: bool


class ArtifactVault:
    """Content-addressed local storage for exact retrieved source bytes."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root).resolve() if root else None

    @property
    def enabled(self) -> bool:
        return self.root is not None

    def store(self, content: bytes, expected_sha256: str | None = None) -> VaultReceipt | None:
        if not self.enabled or not content:
            return None
        digest = sha256(content).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise ValueError("Artifact bytes do not match the acquisition fingerprint")

        target = self._path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        created = not target.exists()
        if created:
            temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
            temporary.write_bytes(content)
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        elif sha256(target.read_bytes()).hexdigest() != digest:
            raise RuntimeError("Artifact vault integrity check failed")
        return VaultReceipt(
            uri=f"vault://sha256/{digest}",
            sha256=digest,
            bytes_stored=len(content),
            created=created,
        )

    def verify(self, uri: str | None, expected_sha256: str | None) -> bool:
        if not self.enabled or uri is None or expected_sha256 is None:
            return False
        prefix = "vault://sha256/"
        if not uri.startswith(prefix):
            return False
        digest = uri.removeprefix(prefix)
        if digest != expected_sha256 or not SHA256_PATTERN.fullmatch(digest):
            return False
        path = self._path(digest)
        return path.is_file() and sha256(path.read_bytes()).hexdigest() == expected_sha256

    def _path(self, digest: str) -> Path:
        if self.root is None or not SHA256_PATTERN.fullmatch(digest):
            raise ValueError("Invalid artifact digest")
        return self.root / digest[:2] / digest[2:4] / digest
