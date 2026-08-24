"""
No live database needed here — the vault and the local-dev KMS provider
are pure Python + filesystem. This is the one test module in the suite
that runs without TEST_DATABASE_URL.
"""
from __future__ import annotations

import uuid

import pytest
from cryptography.exceptions import InvalidTag

from app.credentials.kms import LocalFileKMSProvider, OrgKeyNotFoundError
from app.credentials.vault import CredentialVault


@pytest.fixture
def vault(tmp_path) -> CredentialVault:
    return CredentialVault(kms=LocalFileKMSProvider(str(tmp_path / "kms_keys")))


@pytest.mark.asyncio
async def test_roundtrip_encrypt_decrypt(vault):
    org_id = uuid.uuid4()
    plaintext = b"super-secret-refresh-token"

    fields = await vault.encrypt(org_id=org_id, plaintext=plaintext)

    from app.credentials.models import OrgCredential

    cred = OrgCredential(
        org_id=org_id,
        provider="google",
        credential_type="oauth_refresh_token",
        ciphertext=fields.ciphertext,
        nonce=fields.nonce,
        auth_tag=fields.auth_tag,
        wrapped_data_key=fields.wrapped_data_key,
        key_version=fields.key_version,
    )

    recovered = await vault.decrypt(org_id=org_id, credential=cred)
    assert recovered == plaintext


@pytest.mark.asyncio
async def test_wrong_org_id_fails_to_decrypt(vault):
    """
    §7 of the red-team review: "org_id as AAD... converts a cross-tenant
    credential leak from a silent breach into a decryption failure."
    Encrypt under org A; attempting to decrypt the SAME ciphertext under
    org B's identity must raise, never silently return org A's secret.
    """
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    fields = await vault.encrypt(org_id=org_a, plaintext=b"org-a-secret")

    from app.credentials.models import OrgCredential

    cred = OrgCredential(
        org_id=org_a,
        provider="google",
        credential_type="oauth_refresh_token",
        ciphertext=fields.ciphertext,
        nonce=fields.nonce,
        auth_tag=fields.auth_tag,
        wrapped_data_key=fields.wrapped_data_key,
        key_version=fields.key_version,
    )

    with pytest.raises((InvalidTag, OrgKeyNotFoundError)):
        await vault.decrypt(org_id=org_b, credential=cred)


@pytest.mark.asyncio
async def test_destroying_org_key_makes_ciphertext_permanently_unreadable(vault):
    """
    RC-2 / FF-2's actual fix, proven directly: after `destroy_org_key`,
    even decrypting under the CORRECT org_id must fail — this is the
    property the master plan's original crypto-shredding design did NOT
    have (the wrapped key lived in a DB column that survived every
    backup). Here the key material lives outside this test's "database"
    entirely (a local file, standing in for a real per-org KMS key), and
    deleting it is irreversible.
    """
    org_id = uuid.uuid4()
    fields = await vault.encrypt(org_id=org_id, plaintext=b"about-to-be-deleted")

    from app.credentials.models import OrgCredential

    cred = OrgCredential(
        org_id=org_id,
        provider="twilio",
        credential_type="api_key",
        ciphertext=fields.ciphertext,
        nonce=fields.nonce,
        auth_tag=fields.auth_tag,
        wrapped_data_key=fields.wrapped_data_key,
        key_version=fields.key_version,
    )

    await vault._kms.destroy_org_key(org_id)  # simulates org offboarding (§29)

    with pytest.raises(OrgKeyNotFoundError):
        await vault.decrypt(org_id=org_id, credential=cred)
