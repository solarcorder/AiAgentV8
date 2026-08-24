"""
Per-org key management — RC-2, replacing the master plan's single-master-
key hierarchy after the red team's FF-2 finding:

    The master plan's §29 promised that destroying an org's wrapped data
    key made backed-up ciphertext "permanently unreadable." As specified,
    `wrapped_data_key` was a column in `org_credentials`, which is exactly
    the table that gets backed up nightly. Deleting the row deletes a
    database row; every backup taken before deletion still has it, the
    KMS master key still exists (it wraps every other org's keys too),
    and restoring a backup recovers the "destroyed" credential in full.
    The deletion guarantee was false as written.

The fix: give every org its OWN key, held by the KMS provider, never
persisted as material inside this database at all. Deletion destroys
*that key*, in KMS. A restored backup still contains ciphertext, but
nothing in the universe can unwrap it anymore — which is the property
"permanently unreadable" actually requires.

This module defines the interface and two implementations:

  - GCPKMSProvider / AWSKMSProvider: production shape. Each org gets its
    own CryptoKey (GCP) / KMS key (AWS), addressed by an org-derived key
    ID or alias. Wrap/unwrap calls the provider's API; key material never
    leaves the HSM boundary, ever. NOT IMPLEMENTED here — wire up the
    real SDK before production; the interface is what matters for the
    rest of the codebase to be provider-agnostic.
  - LocalFileKMSProvider: development/test only. Simulates "a key that is
    not in the database backup" by storing per-org key material as files
    under `settings.local_dev_kms_key_dir`, a directory that must be
    excluded from any Postgres backup/restore process. This is NOT a
    security control — a local file has none of the properties a real
    HSM-backed KMS provides — it exists only so the deletion code path
    and its tests behave correctly without cloud credentials.
"""
from __future__ import annotations

import abc
import os
import secrets
import uuid
from pathlib import Path


class KMSProvider(abc.ABC):
    @abc.abstractmethod
    async def wrap_data_key(self, org_id: uuid.UUID, plaintext_key: bytes) -> bytes:
        """Encrypt a 32-byte data key under this org's KEK. Returns the wrapped bytes to store in Postgres."""

    @abc.abstractmethod
    async def unwrap_data_key(self, org_id: uuid.UUID, wrapped_key: bytes) -> bytes:
        """Decrypt a wrapped data key. Raises if the org's key has been destroyed."""

    @abc.abstractmethod
    async def destroy_org_key(self, org_id: uuid.UUID) -> None:
        """
        Irreversibly destroy this org's KEK. After this call, every
        ciphertext ever wrapped under it — including in old database
        backups — becomes permanently unrecoverable. This is the actual
        mechanism behind the customer-facing "your credentials have been
        deleted" claim; see app/credentials/vault.py and §29's rewritten
        deletion language.
        """


class OrgKeyNotFoundError(Exception):
    """The org's KEK does not exist — either never provisioned, or already destroyed (post-deletion)."""


class LocalFileKMSProvider(KMSProvider):
    """
    DEV/TEST ONLY. See module docstring. The key directory must live
    outside whatever directory your local Postgres backup script (if any)
    captures — that is the entire property this class exists to model.
    """

    def __init__(self, key_dir: str) -> None:
        self._dir = Path(key_dir)
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _key_path(self, org_id: uuid.UUID) -> Path:
        return self._dir / f"{org_id}.key"

    def _get_or_create_kek(self, org_id: uuid.UUID) -> bytes:
        path = self._key_path(org_id)
        if path.exists():
            return path.read_bytes()
        kek = secrets.token_bytes(32)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, kek)
        finally:
            os.close(fd)
        return kek

    async def wrap_data_key(self, org_id: uuid.UUID, plaintext_key: bytes) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        kek = self._get_or_create_kek(org_id)
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(kek).encrypt(nonce, plaintext_key, associated_data=str(org_id).encode())
        return nonce + ciphertext  # nonce prefix; AESGCM appends its own 16-byte tag

    async def unwrap_data_key(self, org_id: uuid.UUID, wrapped_key: bytes) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        path = self._key_path(org_id)
        if not path.exists():
            raise OrgKeyNotFoundError(f"no KEK for org {org_id} — destroyed or never provisioned")
        kek = path.read_bytes()
        nonce, ciphertext = wrapped_key[:12], wrapped_key[12:]
        return AESGCM(kek).decrypt(nonce, ciphertext, associated_data=str(org_id).encode())

    async def destroy_org_key(self, org_id: uuid.UUID) -> None:
        path = self._key_path(org_id)
        if path.exists():
            path.unlink()


class GCPKMSProvider(KMSProvider):
    """
    Production shape. Each org maps to its own CryptoKey under a KeyRing,
    e.g. `orgs/{org_id}` — created via the KMS API on first use, destroyed
    (key material scheduled for destruction, then the CryptoKeyVersion
    disabled/destroyed) on org deletion. Left unimplemented here
    deliberately: wiring this up requires a real GCP project, IAM binding
    for the service account, and a decision on key rotation cadence,
    none of which belong hardcoded into a scaffold.
    """

    async def wrap_data_key(self, org_id: uuid.UUID, plaintext_key: bytes) -> bytes:
        raise NotImplementedError("wire up google-cloud-kms before using this in production")

    async def unwrap_data_key(self, org_id: uuid.UUID, wrapped_key: bytes) -> bytes:
        raise NotImplementedError("wire up google-cloud-kms before using this in production")

    async def destroy_org_key(self, org_id: uuid.UUID) -> None:
        raise NotImplementedError("wire up google-cloud-kms before using this in production")


def get_kms_provider() -> KMSProvider:
    from app.config import get_settings

    settings = get_settings()
    if settings.kms_provider == "local_dev":
        return LocalFileKMSProvider(settings.local_dev_kms_key_dir)
    if settings.kms_provider == "gcp_kms":
        return GCPKMSProvider()
    raise ValueError(f"unknown kms_provider: {settings.kms_provider}")
