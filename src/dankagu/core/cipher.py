"""Takasho symmetric block cipher (ChaCha/Salsa variant).

"""

from typing import Final


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

        self._state: list[int] = [0] * 16
        for i in range(4):
            self._state[i] = int.from_bytes(self.SIGMA[4 * i : 4 * (i + 1)], "little")
        for i in range(8):
            self._state[i + 4] = int.from_bytes(key[4 * i : 4 * (i + 1)], "little")
        for i in range(3):
            self._state[i + 13] = int.from_bytes(nonce[4 * i : 4 * (i + 1)], "little")

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
        result = bytearray()
        length = len(body)
        body_offset = 0

        while length > 0:
            block_len = min(length, 0x40)
            state = self._state.copy()
            state[12] = counter

            rounds = 10 - self._round_gap
            if self._round_gap < 10:
                rounds = max(rounds, 1)
                for _ in range(rounds):
                    # Column rounds
                    state[0] = (state[0] + state[4]) & 0xFFFFFFFF
                    state[12] = _rotate_right(state[12] ^ state[0], 0x10)
                    state[8] = (state[8] + state[12]) & 0xFFFFFFFF
                    state[4] = _rotate_right(state[4] ^ state[8], 0x14)
                    state[0] = (state[0] + state[4]) & 0xFFFFFFFF
                    state[12] = _rotate_right(state[12] ^ state[0], 0x18)
                    state[8] = (state[8] + state[12]) & 0xFFFFFFFF
                    state[4] = _rotate_right(state[4] ^ state[8], 0x19)

                    state[1] = (state[1] + state[5]) & 0xFFFFFFFF
                    state[13] = _rotate_right(state[13] ^ state[1], 0x10)
                    state[9] = (state[9] + state[13]) & 0xFFFFFFFF
                    state[5] = _rotate_right(state[5] ^ state[9], 0x14)
                    state[1] = (state[1] + state[5]) & 0xFFFFFFFF
                    state[13] = _rotate_right(state[13] ^ state[1], 0x18)
                    state[9] = (state[9] + state[13]) & 0xFFFFFFFF
                    state[5] = _rotate_right(state[5] ^ state[9], 0x19)

                    state[2] = (state[2] + state[6]) & 0xFFFFFFFF
                    state[14] = _rotate_right(state[14] ^ state[2], 0x10)
                    state[10] = (state[10] + state[14]) & 0xFFFFFFFF
                    state[6] = _rotate_right(state[6] ^ state[10], 0x14)
                    state[2] = (state[2] + state[6]) & 0xFFFFFFFF
                    state[14] = _rotate_right(state[14] ^ state[2], 0x18)
                    state[10] = (state[10] + state[14]) & 0xFFFFFFFF
                    state[6] = _rotate_right(state[6] ^ state[10], 0x19)

                    state[3] = (state[3] + state[7]) & 0xFFFFFFFF
                    state[15] = _rotate_right(state[15] ^ state[3], 0x10)
                    state[11] = (state[11] + state[15]) & 0xFFFFFFFF
                    state[7] = _rotate_right(state[7] ^ state[11], 0x14)
                    state[3] = (state[3] + state[7]) & 0xFFFFFFFF
                    state[15] = _rotate_right(state[15] ^ state[3], 0x18)
                    state[11] = (state[11] + state[15]) & 0xFFFFFFFF
                    state[7] = _rotate_right(state[7] ^ state[11], 0x19)

                    # Diagonal rounds
                    state[0] = (state[0] + state[5]) & 0xFFFFFFFF
                    state[15] = _rotate_right(state[15] ^ state[0], 0x10)
                    state[10] = (state[10] + state[15]) & 0xFFFFFFFF
                    state[5] = _rotate_right(state[5] ^ state[10], 0x14)
                    state[0] = (state[0] + state[5]) & 0xFFFFFFFF
                    state[15] = _rotate_right(state[15] ^ state[0], 0x18)
                    state[10] = (state[10] + state[15]) & 0xFFFFFFFF
                    state[5] = _rotate_right(state[5] ^ state[10], 0x19)

                    state[1] = (state[1] + state[6]) & 0xFFFFFFFF
                    state[12] = _rotate_right(state[12] ^ state[1], 0x10)
                    state[11] = (state[11] + state[12]) & 0xFFFFFFFF
                    state[6] = _rotate_right(state[6] ^ state[11], 0x14)
                    state[1] = (state[1] + state[6]) & 0xFFFFFFFF
                    state[12] = _rotate_right(state[12] ^ state[1], 0x18)
                    state[11] = (state[11] + state[12]) & 0xFFFFFFFF
                    state[6] = _rotate_right(state[6] ^ state[11], 0x19)

                    state[2] = (state[2] + state[7]) & 0xFFFFFFFF
                    state[13] = _rotate_right(state[13] ^ state[2], 0x10)
                    state[8] = (state[8] + state[13]) & 0xFFFFFFFF
                    state[7] = _rotate_right(state[7] ^ state[8], 0x14)
                    state[2] = (state[2] + state[7]) & 0xFFFFFFFF
                    state[13] = _rotate_right(state[13] ^ state[2], 0x18)
                    state[8] = (state[8] + state[13]) & 0xFFFFFFFF
                    state[7] = _rotate_right(state[7] ^ state[8], 0x19)

                    state[3] = (state[3] + state[4]) & 0xFFFFFFFF
                    state[14] = _rotate_right(state[14] ^ state[3], 0x10)
                    state[9] = (state[9] + state[14]) & 0xFFFFFFFF
                    state[4] = _rotate_right(state[4] ^ state[9], 0x14)
                    state[3] = (state[3] + state[4]) & 0xFFFFFFFF
                    state[14] = _rotate_right(state[14] ^ state[3], 0x18)
                    state[9] = (state[9] + state[14]) & 0xFFFFFFFF
                    state[4] = _rotate_right(state[4] ^ state[9], 0x19)

            for i in range(16):
                state[i] = (state[i] + self._state[i]) & 0xFFFFFFFF
            state[12] = (state[12] + counter) & 0xFFFFFFFF

            for i in range(block_len):
                keystream_byte = (state[i // 4] >> (8 * (i % 4))) & 0xFF
                result.append(body[body_offset + i] ^ keystream_byte)

            length -= block_len
            body_offset += block_len
            counter += 1

        return bytes(result)


def _rotate_right(value: int, n_bits: int) -> int:
    """Bitwise 32-bit right rotation."""
    return ((value >> n_bits) | (value << (32 - n_bits))) & 0xFFFFFFFF
