import base64

import pytest

from dankagu.core.cipher import Cipher
from dankagu.core.packer import InvalidMacError, TakashoPackerError, default_packer


def test_cipher_known_vector() -> None:
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
