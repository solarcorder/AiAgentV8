"""
Envelope encryption for per-tenant credentials (§13), with the red team's
one unreservedly praised control built in: `org_id` as AES-GCM additional
authenticated data (AAD).

Why AAD matters here specifically: it means a ciphertext is not merely
*associated* with an org by a foreign key — it is cryptographically bound
to it. If a bug anywhere ever caused an org A credential row to be read
under org B's context (wrong org_id passed in, a join gone wrong,
whatever), decryption does not quietly succeed and hand back the wrong
secret — it fails, loudly, with an authentication error, because the AAD
used to decrypt does not match the AAD used to encrypt. A silent
cross-tenant credential leak becomes a hard decryption failure. This is
called out in the red-team review (§7) as the strongest property in the
whole design and something worth copying into any system doing
per-tenant secrets.

Key hierarchy (RC-2, see app/credentials/kms.py for why this changed from
the master plan's single master key): plaintext credential -> encrypted
with a random one-time 256-bit data key (AES-256-GCM) -> the data key
itself is wrapped by the org's own KMS-held key, never the DB's own
column. Only the wrapped data key is stored in Postgres; the wrapping key
never is.
"""
from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.credentials.kms import KMSProvider, get_kms_provider
from app.credentials.models import OrgCredential

_AESGCM_TAG_LENGTH = 16


@dataclass(frozen=True, slots=True)
class EncryptedFields:
    ciphertext: bytes
    nonce: bytes
    auth_tag: bytes
    wrapped_data_key: bytes
    key_version: int


class CredentialVault:
    """
    Decryption happens in memory, for the duration of one outbound call,
    and the plaintext is never assigned to anything that outlives that
    call (no caching, no logging, no returning it up through a generic
    "get credential" endpoint). Callers should use `decrypt_for_call`
    below rather than calling `decrypt` directly where practical, as a
    reminder of that lifetime discipline.
    """

    def __init__(self, kms: KMSProvider | None = None) -> None:
        self._kms = kms or get_kms_provider()

    async def encrypt(
        self, *, org_id: uuid.UUID, plaintext: bytes, key_version: int = 1
    ) -> EncryptedFields:
        data_key = secrets.token_bytes(32)
        nonce = secrets.token_bytes(12)
        aad = str(org_id).encode()

        sealed = AESGCM(data_key).encrypt(nonce, plaintext, associated_data=aad)
        ciphertext, auth_tag = sealed[:-_AESGCM_TAG_LENGTH], sealed[-_AESGCM_TAG_LENGTH:]

        wrapped_data_key = await self._kms.wrap_data_key(org_id, data_key)

        return EncryptedFields(
            ciphertext=ciphertext,
            nonce=nonce,
            auth_tag=auth_tag,
            wrapped_data_key=wrapped_data_key,
            key_version=key_version,
        )

    async def decrypt(self, *, org_id: uuid.UUID, credential: OrgCredential) -> bytes:
        """
        Raises `cryptography.exceptions.InvalidTag` if `org_id` does not
        match the org this credential was encrypted for, or if the
        ciphertext/tag/wrapped key have been tampered with. Raises
        `OrgKeyNotFoundError` (see kms.py) if the org's key has been
        destroyed — i.e. the org was deleted per §29's crypto-shredding
        flow, and this is the guarantee working as designed, not a bug.
        """
        data_key = await self._kms.unwrap_data_key(org_id, credential.wrapped_data_key)
        aad = str(org_id).encode()
        sealed = credential.ciphertext + credential.auth_tag
        return AESGCM(data_key).decrypt(credential.nonce, sealed, associated_data=aad)

    async def rewrap_for_org_id_verification(self, org_id: uuid.UUID, credential: OrgCredential) -> bool:
        """Cheap check used by tests: does this credential decrypt under this org_id? No side effects."""
        try:
            await self.decrypt(org_id=org_id, credential=credential)
            return True
        except Exception:
            return False
