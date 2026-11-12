"""Symmetric encryption for secrets stored in the database (currently:
auth_providers.client_secret_encrypted).

The encryption key is derived from DASHBOARD_SESSION_SECRET (SHA-256 ->
32 raw bytes -> urlsafe-base64, the shape Fernet requires) rather than a
brand-new required env var, so operators who already set
DASHBOARD_SESSION_SECRET (recommended so sessions survive restarts) get
stable, working encryption for free. If DASHBOARD_SESSION_SECRET is ever
unset/changed, previously-stored provider secrets become undecryptable and
must be re-entered via the admin UI -- the same restart-sensitivity caveat
that already applies to sessions.
"""

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from src.utils.config import Config


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = hashlib.sha256(Config.SESSION_SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret (e.g. an OAuth client secret) for storage in the DB."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    """Decrypt a secret previously encrypted with encrypt_secret().

    Raises ValueError (not the lower-level InvalidToken) if the token can't
    be decrypted -- most commonly because DASHBOARD_SESSION_SECRET changed
    since it was stored."""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError(
            "Could not decrypt stored secret -- DASHBOARD_SESSION_SECRET may "
            "have changed since it was saved. Re-enter the client secret for "
            "this provider."
        ) from exc
