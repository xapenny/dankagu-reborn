"""Takasho symmetric block cipher (ChaCha/Salsa variant)."""

import struct
from typing import Final

_UNPACK_4I = struct.Struct("<4I").unpack
_UNPACK_8I = struct.Struct("<8I").unpack
_UNPACK_3I = struct.Struct("<3I").unpack
_PACK_16I = struct.Struct("<16I").pack


class Cipher:
    """Symmetric 16-word state block cipher

    The state consists of 16 32-bit unsigned integers initialized from:
    - 4 words: sigma constant
    - 8 words: 256-bit key
    - 1 word: block counter (at index 12)
    - 3 words: 96-bit nonce (at indices 13, 14, 15)
    """

    SIGMA: Final[bytes] = b"zG]RTQVC{zkpV]/T"
    ROUND_GAPS: Final[tuple[int, ...]] = (1, 3, 4, 1, 1, 3, 3, 2, 4, 3, 2, 2, 1, 4, 4, 2)

    def __init__(self, key: bytes, nonce: bytes) -> None:
        """Initialize the block cipher with a 32-byte key and 12-byte nonce.

        Args:
            key: 32 bytes secret key.
            nonce: 12 bytes nonce.
        """
        if len(key) != 32:
            raise ValueError(f"Key must be 32 bytes, got {len(key)}")
        if len(nonce) != 12:
            raise ValueError(f"Nonce must be 12 bytes, got {len(nonce)}")

        self._state: list[int] = [
            *_UNPACK_4I(self.SIGMA),
            *_UNPACK_8I(key),
            0,
            *_UNPACK_3I(nonce),
        ]

        u = (
            ((self._state[9] + self._state[4]) ^ self._state[11])
            + ((self._state[14] + self._state[13]) ^ self._state[15])
        ) & 0xFFFFFFFF
        gap_idx = ((u >> 7) & 2) | ((u >> 2) & 1) | ((u >> 13) & 4) | ((u >> 2) & 8)
        self._round_gap = self.ROUND_GAPS[gap_idx]

    def transform(self, body: bytes, counter: int = 0) -> bytes:
        """Encrypt or decrypt the input body (XOR operation is symmetric).

        Args:
            body: Input data bytes to transform.
            counter: Initial block counter value (defaults to 0).

        Returns:
            The transformed bytes.
        """
        if not body:
            return b""

        i0, i1, i2, i3, i4, i5, i6, i7, i8, i9, i10, i11, _, i13, i14, i15 = self._state
        round_gap = self._round_gap
        rounds = max(10 - round_gap, 1) if round_gap < 10 else 0
        pack_16i = _PACK_16I
        from_bytes = int.from_bytes

        chunks: list[bytes] = []
        mv = memoryview(body)
        total_len = len(body)
        offset = 0

        while offset < total_len:
            s0, s1, s2, s3 = i0, i1, i2, i3
            s4, s5, s6, s7 = i4, i5, i6, i7
            s8, s9, s10, s11 = i8, i9, i10, i11
            s12 = counter & 0xFFFFFFFF
            s13, s14, s15 = i13, i14, i15

            for _ in range(rounds):
                # Column rounds
                s0 = (s0 + s4) & 0xFFFFFFFF
                t = s12 ^ s0
                s12 = ((t >> 16) | (t << 16)) & 0xFFFFFFFF
                s8 = (s8 + s12) & 0xFFFFFFFF
                t = s4 ^ s8
                s4 = ((t >> 20) | (t << 12)) & 0xFFFFFFFF
                s0 = (s0 + s4) & 0xFFFFFFFF
                t = s12 ^ s0
                s12 = ((t >> 24) | (t << 8)) & 0xFFFFFFFF
                s8 = (s8 + s12) & 0xFFFFFFFF
                t = s4 ^ s8
                s4 = ((t >> 25) | (t << 7)) & 0xFFFFFFFF

                s1 = (s1 + s5) & 0xFFFFFFFF
                t = s13 ^ s1
                s13 = ((t >> 16) | (t << 16)) & 0xFFFFFFFF
                s9 = (s9 + s13) & 0xFFFFFFFF
                t = s5 ^ s9
                s5 = ((t >> 20) | (t << 12)) & 0xFFFFFFFF
                s1 = (s1 + s5) & 0xFFFFFFFF
                t = s13 ^ s1
                s13 = ((t >> 24) | (t << 8)) & 0xFFFFFFFF
                s9 = (s9 + s13) & 0xFFFFFFFF
                t = s5 ^ s9
                s5 = ((t >> 25) | (t << 7)) & 0xFFFFFFFF

                s2 = (s2 + s6) & 0xFFFFFFFF
                t = s14 ^ s2
                s14 = ((t >> 16) | (t << 16)) & 0xFFFFFFFF
                s10 = (s10 + s14) & 0xFFFFFFFF
                t = s6 ^ s10
                s6 = ((t >> 20) | (t << 12)) & 0xFFFFFFFF
                s2 = (s2 + s6) & 0xFFFFFFFF
                t = s14 ^ s2
                s14 = ((t >> 24) | (t << 8)) & 0xFFFFFFFF
                s10 = (s10 + s14) & 0xFFFFFFFF
                t = s6 ^ s10
                s6 = ((t >> 25) | (t << 7)) & 0xFFFFFFFF

                s3 = (s3 + s7) & 0xFFFFFFFF
                t = s15 ^ s3
                s15 = ((t >> 16) | (t << 16)) & 0xFFFFFFFF
                s11 = (s11 + s15) & 0xFFFFFFFF
                t = s7 ^ s11
                s7 = ((t >> 20) | (t << 12)) & 0xFFFFFFFF
                s3 = (s3 + s7) & 0xFFFFFFFF
                t = s15 ^ s3
                s15 = ((t >> 24) | (t << 8)) & 0xFFFFFFFF
                s11 = (s11 + s15) & 0xFFFFFFFF
                t = s7 ^ s11
                s7 = ((t >> 25) | (t << 7)) & 0xFFFFFFFF

                # Diagonal rounds
                s0 = (s0 + s5) & 0xFFFFFFFF
                t = s15 ^ s0
                s15 = ((t >> 16) | (t << 16)) & 0xFFFFFFFF
                s10 = (s10 + s15) & 0xFFFFFFFF
                t = s5 ^ s10
                s5 = ((t >> 20) | (t << 12)) & 0xFFFFFFFF
                s0 = (s0 + s5) & 0xFFFFFFFF
                t = s15 ^ s0
                s15 = ((t >> 24) | (t << 8)) & 0xFFFFFFFF
                s10 = (s10 + s15) & 0xFFFFFFFF
                t = s5 ^ s10
                s5 = ((t >> 25) | (t << 7)) & 0xFFFFFFFF

                s1 = (s1 + s6) & 0xFFFFFFFF
                t = s12 ^ s1
                s12 = ((t >> 16) | (t << 16)) & 0xFFFFFFFF
                s11 = (s11 + s12) & 0xFFFFFFFF
                t = s6 ^ s11
                s6 = ((t >> 20) | (t << 12)) & 0xFFFFFFFF
                s1 = (s1 + s6) & 0xFFFFFFFF
                t = s12 ^ s1
                s12 = ((t >> 24) | (t << 8)) & 0xFFFFFFFF
                s11 = (s11 + s12) & 0xFFFFFFFF
                t = s6 ^ s11
                s6 = ((t >> 25) | (t << 7)) & 0xFFFFFFFF

                s2 = (s2 + s7) & 0xFFFFFFFF
                t = s13 ^ s2
                s13 = ((t >> 16) | (t << 16)) & 0xFFFFFFFF
                s8 = (s8 + s13) & 0xFFFFFFFF
                t = s7 ^ s8
                s7 = ((t >> 20) | (t << 12)) & 0xFFFFFFFF
                s2 = (s2 + s7) & 0xFFFFFFFF
                t = s13 ^ s2
                s13 = ((t >> 24) | (t << 8)) & 0xFFFFFFFF
                s8 = (s8 + s13) & 0xFFFFFFFF
                t = s7 ^ s8
                s7 = ((t >> 25) | (t << 7)) & 0xFFFFFFFF

                s3 = (s3 + s4) & 0xFFFFFFFF
                t = s14 ^ s3
                s14 = ((t >> 16) | (t << 16)) & 0xFFFFFFFF
                s9 = (s9 + s14) & 0xFFFFFFFF
                t = s4 ^ s9
                s4 = ((t >> 20) | (t << 12)) & 0xFFFFFFFF
                s3 = (s3 + s4) & 0xFFFFFFFF
                t = s14 ^ s3
                s14 = ((t >> 24) | (t << 8)) & 0xFFFFFFFF
                s9 = (s9 + s14) & 0xFFFFFFFF
                t = s4 ^ s9
                s4 = ((t >> 25) | (t << 7)) & 0xFFFFFFFF

            ks = pack_16i(
                (s0 + i0) & 0xFFFFFFFF,
                (s1 + i1) & 0xFFFFFFFF,
                (s2 + i2) & 0xFFFFFFFF,
                (s3 + i3) & 0xFFFFFFFF,
                (s4 + i4) & 0xFFFFFFFF,
                (s5 + i5) & 0xFFFFFFFF,
                (s6 + i6) & 0xFFFFFFFF,
                (s7 + i7) & 0xFFFFFFFF,
                (s8 + i8) & 0xFFFFFFFF,
                (s9 + i9) & 0xFFFFFFFF,
                (s10 + i10) & 0xFFFFFFFF,
                (s11 + i11) & 0xFFFFFFFF,
                (s12 + counter) & 0xFFFFFFFF,
                (s13 + i13) & 0xFFFFFFFF,
                (s14 + i14) & 0xFFFFFFFF,
                (s15 + i15) & 0xFFFFFFFF,
            )

            block = mv[offset : offset + 64]
            blen = len(block)
            if blen == 64:
                chunks.append(
                    (from_bytes(block, "little") ^ from_bytes(ks, "little")).to_bytes(64, "little")
                )
            else:
                chunks.append(
                    (from_bytes(block, "little") ^ from_bytes(ks[:blen], "little")).to_bytes(
                        blen, "little"
                    )
                )

            offset += 64
            counter += 1

        return b"".join(chunks)


def _rotate_right(value: int, n_bits: int) -> int:
    """Bitwise 32-bit right rotation."""
    return ((value >> n_bits) | (value << (32 - n_bits))) & 0xFFFFFFFF
