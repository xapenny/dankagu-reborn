import base64

import pytest

from dankagu.core.cipher import Cipher
from dankagu.core.packer import (
    InvalidMacError,
    TakashoPacker,
    TakashoPackerError,
    default_packer,
)


def test_cipher_known_vector() -> None:
    # Synthetic known-answer key for this test only. It is passed straight to
    # Cipher(...) and is unrelated to DANKAGU_TAKASHO_KEY_HEX (production) and
    # to the public development key. Not a secret; safe to publish.
    key = bytes(
        [
            0x7C,
            0xF8,
            0xB8,
            0xBE,
            0xF6,
            0x61,
            0x1A,
            0xC7,
            0x03,
            0x05,
            0x18,
            0xE8,
            0xF1,
            0x06,
            0xD7,
            0xA1,
            0x26,
            0x54,
            0x9B,
            0x5B,
            0x91,
            0x41,
            0xE3,
            0x19,
            0xFF,
            0xF2,
            0xBF,
            0x24,
            0xC8,
            0xB3,
            0x64,
            0x42,
        ]
    )
    nonce = bytes(
        [
            0x7A,
            0x44,
            0x92,
            0x5C,
            0x2F,
            0xC6,
            0x13,
            0x83,
            0x2C,
            0xDF,
            0xF4,
            0x5E,
        ]
    )
    cipher = Cipher(key, nonce)
    plaintext = bytes(
        [
            0xCE,
            0x01,
            0xB3,
            0xE9,
            0x71,
            0x9F,
            0x82,
            0xCB,
            0x96,
            0x5E,
            0xDB,
            0xEB,
            0x49,
            0xA4,
            0x10,
            0x0E,
            0xA3,
            0x71,
            0x58,
            0x8A,
            0x84,
            0x8D,
            0x48,
            0xF0,
            0xD9,
            0x9C,
            0x13,
            0x61,
            0x6C,
            0xBE,
            0x54,
            0x8D,
        ]
    )
    ciphertext = cipher.transform(plaintext)
    assert base64.b64encode(ciphertext) == b"m4hXJUSxgBtI/hLhNtkOXBZqca7NdcDTiDunnA+JJa4="

    # Cipher is symmetric: transform again recovers original plaintext
    decrypted = cipher.transform(ciphertext)
    assert decrypted == plaintext


def test_packer_known_vector() -> None:
    proto_hex = (
        "0a0f4f444d47656e657269635f426f6f740a154f444d47656e657269635f5365637572"
        "65486173680a144f444d47656e657269635f5369676e6174757265"
    )
    packed_hex = (
        "87deaf4ba0958a38dab01da2ee358fac144b45e3cccdf195dabd38a4ec9429a621f559"
        "71b9e48fc3fa850ee1b8f467a8cbc4a1c32287ed20dc32682b1f6d17587390e657d0"
        "a7fa91c5e0a6e1cc1169e9961d51a6aef3a19aca873f80"
    )
    unpacked = default_packer.unpack(bytes.fromhex(packed_hex))
    assert unpacked.hex() == proto_hex

    repacked = default_packer.pack(unpacked)
    roundtrip = default_packer.unpack(repacked)
    assert roundtrip.hex() == proto_hex


def test_packer_invalid_mac() -> None:
    proto_bytes = b"Hello Takasho"
    packed = bytearray(default_packer.pack(proto_bytes))
    # Corrupting ciphertext raises TakashoPackerError (e.g. decompression failure or MAC mismatch)
    packed[15] ^= 0xFF
    with pytest.raises(TakashoPackerError):
        default_packer.unpack(bytes(packed))

    # Test forged HMAC with valid deflate stream
    import zlib

    from dankagu.core.cipher import Cipher

    nonce = b"0123456789ab"
    bad_hmac = b"\x00" * 32
    deflated = zlib.compress(bad_hmac + proto_bytes)[2:-4]
    cipher = Cipher(default_packer._key, nonce)
    forged_framed = nonce + cipher.transform(deflated)
    with pytest.raises(InvalidMacError):
        default_packer.unpack(forged_framed)


# ---------------------------------------------------------------------------
# Production-key path.
#
# Tests elsewhere exercise the packer through the module-level `default_packer`,
# which in a clean environment has no production key and therefore always
# succeeds via the *fallback* key. That leaves the primary key path - the one a
# real operator runs - untested. These tests pin it down directly.
# ---------------------------------------------------------------------------

# Arbitrary 32-byte keys: the cipher and HMAC accept any key material, so a
# roundtrip is a real assertion without needing a genuine client key.
_PROD_LIKE_KEY = bytes(range(0x20, 0x40))
_OTHER_KEY = bytes(range(0x60, 0x80))


def test_packer_roundtrip_with_explicit_key() -> None:
    packer = TakashoPacker(key=_PROD_LIKE_KEY)
    assert packer._active_key == _PROD_LIKE_KEY

    body = b"\x0a\x03takasho\x12\x02ok"
    framed = packer.pack(body)

    # 12-byte nonce prefix, and the ciphertext must not equal the plaintext.
    assert len(framed) > 12
    assert framed[12:] != body

    assert packer.unpack(framed) == body


def test_packer_roundtrip_with_runtime_supplied_production_key() -> None:
    """The realistic deployment shape: a production key plus a dev fallback."""
    packer = TakashoPacker(key=_PROD_LIKE_KEY, fallback_keys=[b"ZA1Cu0eZosC3o8YTFuGjloxRkCg6ugVv"])
    body = b"payload"

    framed = packer.pack(body)

    # The frame was written with the production key, so it must be readable by
    # a packer configured with that key alone.
    assert TakashoPacker(key=_PROD_LIKE_KEY).unpack(framed) == body
    assert packer.unpack(framed) == body


def test_packer_wrong_key_without_fallback_is_rejected() -> None:
    framed = TakashoPacker(key=_PROD_LIKE_KEY).pack(b"secret payload")

    # No fallback configured, so there is nothing that can rescue this frame.
    with pytest.raises(TakashoPackerError):
        TakashoPacker(key=_OTHER_KEY).unpack(framed)


def test_packer_falls_back_to_secondary_key() -> None:
    """A frame written with the fallback key is still accepted when it is listed."""
    framed = TakashoPacker(key=_OTHER_KEY).pack(b"written with the fallback key")

    packer = TakashoPacker(key=_PROD_LIKE_KEY, fallback_keys=[_OTHER_KEY])
    assert packer.unpack(framed) == b"written with the fallback key"
    # The packer remembers which key worked, so later packs use it.
    assert packer._active_key == _OTHER_KEY


def test_compute_hmac_binds_body_and_nonce() -> None:
    packer = TakashoPacker(key=_PROD_LIKE_KEY)
    nonce = b"0123456789ab"  # must be exactly 12 bytes
    mac = packer.compute_hmac(nonce, b"body")

    assert len(mac) == 32
    # Changing either input changes the MAC: the nonce is part of the key
    # material, not merely a prefix on the wire.
    assert packer.compute_hmac(nonce, b"other body") != mac
    assert packer.compute_hmac(b"0123456789ac", b"body") != mac
