"""Takasho message framing codec (Packer/Unpacker).

Wraps Protobuf payloads in:
1. 12-byte Nonce
2. Custom Salsa/ChaCha block cipher
3. Raw Deflate compression
4. 32-byte HMAC-SHA256 verification header
"""

import hashlib
import hmac
import secrets
import zlib
from typing import Final

from dankagu.core.cipher import Cipher


class TakashoPackerError(Exception):
    """Base error for Takasho packing/unpacking failures."""


class InvalidMacError(TakashoPackerError):
    """Raised when HMAC verification fails on unpacked payload."""


class TakashoPacker:
    """Packer/Unpacker codec for Takasho gRPC framing."""

    NONCE_SIZE: Final[int] = 12
    HMAC_SIZE: Final[int] = 32

    def __init__(self, key: bytes, fallback_keys: list[bytes] | None = None) -> None:
        """Initialize the packer with primary and optional fallback keys (32 bytes each)."""
        if len(key) != 32:
            raise ValueError(f"Key must be 32 bytes, got {len(key)}")
        self._key: bytes = key
        self._fallback_keys: list[bytes] = [
            k for k in (fallback_keys or []) if len(k) == 32 and k != key
        ]
        self._active_key: bytes = key

    def compute_hmac(self, nonce: bytes, body: bytes, key: bytes | None = None) -> bytes:
        """Calculate HMAC-SHA256 over body using (key + nonce)."""
        k = key or self._active_key
        mac_key = k + nonce
        return hmac.new(mac_key, body, hashlib.sha256).digest()

    def pack(self, body: bytes, nonce: bytes | None = None) -> bytes:
        """Pack (HMAC + raw deflate + encrypt + prepend nonce).

        Args:
            body: Uncompressed payload (serialized protobuf message).
            nonce: Optional 12-byte nonce. If None, random nonce is generated.

        Returns:
            Framed binary bytes: [12-byte nonce] + [ciphertext].
        """
        if nonce is None:
            nonce = secrets.token_bytes(self.NONCE_SIZE)
        elif len(nonce) != self.NONCE_SIZE:
            raise ValueError(f"Nonce must be {self.NONCE_SIZE} bytes")

        mac = self.compute_hmac(nonce, body, self._active_key)
        # Raw deflate: strip 2-byte zlib header and 4-byte Adler32 checksum
        deflated = zlib.compress(mac + body)[2:-4]
        cipher = Cipher(self._active_key, nonce)
        encrypted = cipher.transform(deflated)
        return nonce + encrypted

    def _unpack_with_key(self, framed_data: bytes, key: bytes) -> bytes:
        nonce = framed_data[: self.NONCE_SIZE]
        ciphertext = framed_data[self.NONCE_SIZE :]

        cipher = Cipher(key, nonce)
        deflated = cipher.transform(ciphertext)

        try:
            decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
            decompressed = decompressor.decompress(deflated)
        except zlib.error as exc:
            raise TakashoPackerError(f"Decompression error: {exc}") from exc

        if len(decompressed) < self.HMAC_SIZE:
            raise TakashoPackerError("Decompressed payload shorter than HMAC header")

        expected_mac = decompressed[: self.HMAC_SIZE]
        body = decompressed[self.HMAC_SIZE :]

        calculated_mac = self.compute_hmac(nonce, body, key)
        if not hmac.compare_digest(expected_mac, calculated_mac):
            raise InvalidMacError("Invalid HMAC detected on unpacked Takasho payload")

        return body

    def unpack(self, framed_data: bytes) -> bytes:
        """Unpack (extract nonce + decrypt + raw inflate + verify HMAC).

        Tries the primary key, followed by fallback keys if HMAC/decompression fails.

        Args:
            framed_data: Framed binary bytes from gRPC request.

        Returns:
            Uncompressed original payload (serialized protobuf message).

        Raises:
            TakashoPackerError: If data is malformed.
            InvalidMacError: If HMAC checksum does not match for all candidate keys.
        """
        if len(framed_data) < self.NONCE_SIZE:
            raise TakashoPackerError(
                f"Data too short ({len(framed_data)} bytes), minimum {self.NONCE_SIZE} bytes"
            )

        candidate_keys = [self._active_key] + [
            k for k in [self._key] + self._fallback_keys if k != self._active_key
        ]
        last_err: Exception | None = None

        for k in candidate_keys:
            try:
                body = self._unpack_with_key(framed_data, k)
                self._active_key = k
                return body
            except (TakashoPackerError, zlib.error) as exc:
                last_err = exc
                continue

        if last_err is not None:
            raise last_err
        raise TakashoPackerError("Failed to unpack payload with all candidate keys")


def _get_default_packer() -> TakashoPacker:
    """Build the default packer.

    The production transport key is supplied at runtime via
    ``DANKAGU_TAKASHO_KEY_HEX`` and is never hardcoded in this repository. When
    it is absent we fall back to the public development key, which is only
    valid for development clients; ``Settings.warn_on_insecure_defaults``
    surfaces this at startup.
    """
    from dankagu.config import settings

    dev_key = settings.takasho_dev_key
    prod_key = settings.takasho_key
    return TakashoPacker(key=prod_key or dev_key, fallback_keys=[dev_key])


# Default packer: runtime production key (if configured) with dev key fallback
default_packer = _get_default_packer()
