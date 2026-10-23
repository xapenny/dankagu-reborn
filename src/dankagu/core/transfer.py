"""Cryptographic utilities for account transfer credentials."""

import hashlib
import hmac
import secrets


def generate_transfer_id() -> str:
    """Generate an 8-character uppercase alphanumeric transfer ID."""
    # Prefix with DK to denote DanKagu, followed by 6 random unambiguous chars
    chars = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    random_part = "".join(secrets.choice(chars) for _ in range(6))
    return f"DK{random_part}"


def generate_transfer_password() -> str:
    """Generate an 8-character secure transfer password."""
    chars = "23456789abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ"
    return "".join(secrets.choice(chars) for _ in range(8))


def hash_transfer_password(password: str) -> str:
    """Hash a transfer password using PBKDF2-HMAC-SHA256 with random salt."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return f"{salt.hex()}:{dk.hex()}"


def verify_transfer_password(password: str, password_hash: str) -> bool:
    """Verify a transfer password against a stored PBKDF2 hash."""
    try:
        salt_hex, dk_hex = password_hash.split(":")
        salt = bytes.fromhex(salt_hex)
        expected_dk = bytes.fromhex(dk_hex)
        actual_dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
        return hmac.compare_digest(actual_dk, expected_dk)
    except Exception:
        return False
