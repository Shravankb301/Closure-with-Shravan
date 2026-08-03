from __future__ import annotations

from hashlib import sha256

import pytest

from evidence_delta.artifact_vault import ArtifactVault


def test_artifact_vault_is_content_addressed_and_verifiable(tmp_path) -> None:
    vault = ArtifactVault(tmp_path / "vault")
    content = b"exact official source bytes"
    digest = sha256(content).hexdigest()

    first = vault.store(content, digest)
    second = vault.store(content, digest)

    assert first is not None
    assert second is not None
    assert first.created is True
    assert second.created is False
    assert first.uri == f"vault://sha256/{digest}"
    assert vault.verify(first.uri, digest) is True


def test_artifact_vault_rejects_a_mismatched_fingerprint(tmp_path) -> None:
    vault = ArtifactVault(tmp_path / "vault")

    with pytest.raises(ValueError, match="fingerprint"):
        vault.store(b"content", "0" * 64)
