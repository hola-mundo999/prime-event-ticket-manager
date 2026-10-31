import pytest
from cryptography.fernet import Fernet


@pytest.fixture(autouse=True)
def temporary_totp_encryption_key(monkeypatch):
    monkeypatch.setenv(
        "ADMIN_TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
