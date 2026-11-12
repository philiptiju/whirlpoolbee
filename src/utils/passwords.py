"""Password hashing for admin-created local (username/password) accounts.

Uses stdlib-only PBKDF2-HMAC-SHA256 (hashlib.pbkdf2_hmac) rather than adding
a bcrypt/argon2 dependency -- this is a small, self-hosted analytics tool,
not a high-value target, and PBKDF2-SHA256 with a modern iteration count is
still an OWASP-recommended choice.
"""

import hashlib
import hmac
import os

_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 260_000  # OWASP 2023 minimum recommendation for PBKDF2-SHA256
_SALT_BYTES = 16


def hash_password(password: str) -> str:
    """Hash `password` for storage. Format: pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>."""
    salt = os.urandom(_SALT_BYTES)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{_ALGORITHM}${_ITERATIONS}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time check of `password` against a hash_password() output."""
    try:
        algorithm, iterations_str, salt_hex, hash_hex = (stored_hash or "").split("$")
        if algorithm != _ALGORITHM:
            return False
        iterations = int(iterations_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(derived, expected)
