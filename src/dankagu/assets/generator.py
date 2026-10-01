"""Generator for Danmaku Kagura ondemand asset manifests (arg_m_* and arg_t_*).

Compiles AssetPath FlatBuffers tables, packs them into single-entry POSIX TAR
archives, compresses them with Gzip, and applies the lightweight XOR stream
cipher (encryptor ID 1004) the client expects on these archives.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
from pathlib import Path
from typing import Any

import flatbuffers

from dankagu.config import settings

logger = logging.getLogger("dankagu.assets.generator")


def make_xorshift_lightweight(seed: int, spin: int, size: int = 512) -> tuple[bytes, int, int]:
    """Generates the swizzle table, coef, and offset from the PRNG seed."""
    state = seed & 0x7FFFFFFF

    def next_val() -> int:
        nonlocal state
        x = state
        x = (x ^ ((x << 13) & 0xFFFFFFFF)) & 0xFFFFFFFF
        x = (x ^ (x >> 17)) & 0xFFFFFFFF
        x = (x ^ ((x << 5) & 0xFFFFFFFF)) & 0xFFFFFFFF
        state = x
        return x

    for _ in range(spin):
        next_val()

    table = bytearray(size)
    for i in range(size):
        table[i] = (next_val() >> 3) & 0xFF

    coef = (next_val() & 0xF) + 3
    offset = (next_val() & 0x1F) + 1
    return bytes(table), coef, offset


# Precompute parameters for LightWeightEncryptor 1004 (seed=0x341a92a9, spin=17)
TABLE_1004, COEF_1004, OFFSET_1004 = make_xorshift_lightweight(0x341A92A9, 17, 512)

# Precompute parameters for LightWeightEncryptor 2001 (seed=0x051c1d53, spin=17, downloaded asset bundles)
TABLE_2001, COEF_2001, OFFSET_2001 = make_xorshift_lightweight(0x051C1D53, 17, 512)


def modify_lightweight(
    data: bytes | bytearray, salt: int, stream_offset: int = 0, data_offset: int = 0
) -> bytes:
    """XOR stream cipher used by LightWeightEncryptor 1004 (symmetric encrypt/decrypt)."""
    out = bytearray(data)
    count = len(out) - data_offset
    mask = len(TABLE_1004) - 1
    for i in range(count):
        pos = data_offset + stream_offset + salt + i
        idx = (pos * COEF_1004 + OFFSET_1004) & mask
        out[data_offset + i] ^= TABLE_1004[idx]
    return bytes(out)


def modify_lightweight_2001(
    data: bytes | bytearray, salt: int, stream_offset: int = 0, data_offset: int = 0
) -> bytes:
    """XOR stream cipher used by LightWeightEncryptor 2001 (downloaded asset bundles)."""
    out = bytearray(data)
    count = len(out) - data_offset
    mask = len(TABLE_2001) - 1
    for i in range(count):
        pos = data_offset + stream_offset + salt + i
        idx = (pos * COEF_2001 + OFFSET_2001) & mask
        out[data_offset + i] ^= TABLE_2001[idx]
    return bytes(out)


def build_asset_path_fb(entries: list[dict[str, Any]]) -> bytes:
    """Serialize AssetPathRaw FlatBuffers binary for the given list of entries."""
    builder = flatbuffers.Builder(1024 * 1024 * 8)
    entry_offsets: list[int] = []

    # FlatBuffers prepends, so reverse to maintain original order
    for item in reversed(entries):
        s_ap = builder.CreateString(str(item["AssetPath"]))
        s_hp = builder.CreateString(str(item["HashedPath"]))
        s_fh = builder.CreateString(str(item["FileHash"]))

        builder.StartObject(8)
        builder.PrependInt64Slot(0, int(item["ID"]), 0)
        builder.PrependUOffsetTRelativeSlot(1, s_ap, 0)
        builder.PrependUOffsetTRelativeSlot(2, s_hp, 0)
        builder.PrependUOffsetTRelativeSlot(3, s_fh, 0)
        builder.PrependInt32Slot(4, int(item.get("FileSize", 0)), 0)
        builder.PrependInt32Slot(5, int(item.get("FileRev", 0)), 0)
        builder.PrependInt32Slot(6, int(item.get("FileType", 0)), 0)
        builder.PrependInt32Slot(7, int(item.get("DownloadOption", 0)), 0)
        entry_offsets.append(builder.EndObject())

    builder.StartVector(4, len(entry_offsets), 4)
    for off in entry_offsets:
        builder.PrependUOffsetTRelative(off)
    vec = builder.EndVector()

    # Root table AssetPathRaw
    builder.StartObject(1)
    builder.PrependUOffsetTRelativeSlot(0, vec, 0)
    root = builder.EndObject()

    builder.Finish(root)
    return bytes(builder.Output())


def make_tar_entry(name: str, data: bytes) -> bytes:
    """Create a single-entry POSIX TAR archive compatible with TarFile.Load.

    Avoids trailing zero blocks to prevent out-of-bounds parsing in the client.
    """
    header = bytearray(512)
    b_name = name.encode("ascii")
    header[0 : len(b_name)] = b_name
    header[100:108] = b"0000644\x00"
    header[108:116] = b"0000000\x00"
    header[116:124] = b"0000000\x00"
    header[124:136] = f"{len(data):011o}\x00".encode("ascii")
    header[136:148] = b"00000000000\x00"
    header[148:156] = b"        "
    header[156] = ord("0")
    header[257:263] = b"ustar\x00"
    header[263:265] = b"00"
    chk = sum(header)
    header[148:156] = f"{chk:06o}\x00 ".encode("ascii")

    pad_len = (512 - (len(data) % 512)) % 512
    return bytes(header) + data + (b"\x00" * pad_len)


def create_manifest_tar_gz(entries: list[dict[str, Any]], salt: int) -> bytes:
    """Create an encrypted tar.gz archive in the format the client expects."""
    fb_data = build_asset_path_fb(entries)
    raw_tar = make_tar_entry("AssetPath", fb_data)

    gz_buf = io.BytesIO()
    with gzip.GzipFile(fileobj=gz_buf, mode="wb", mtime=0) as gz:
        gz.write(raw_tar)
    tar_gz = gz_buf.getvalue()

    return modify_lightweight(tar_gz, salt)


def generate_all_manifest_assets(force: bool = True) -> dict[str, Path]:
    """Pre-generate all arg_m_* and arg_t_* manifest archives for version 3760.

    Returns:
        Mapping of relative hash paths to their generated file paths on disk.
    """
    manifest_json_path = settings.data_dir / "manifest" / "3760-20221018201621-0358.json"
    if not manifest_json_path.exists():
        logger.warning("Manifest json not found at %s, skipping generation", manifest_json_path)
        return {}

    logger.info("Loading manifest catalog from %s", manifest_json_path)
    with manifest_json_path.open("r", encoding="utf-8") as f:
        manifest_data = json.load(f)

    manifest_map = manifest_data.get("manifest", {})

    # Manifest specifications for version 3760 (from ondemand_master.AssetUploadV2)
    specs = [
        # DB (Category 1)
        {
            "category": "4",
            "salt": 2858,
            "hash_path": "4/40/3760-2-4d57879999f6fce80d6d50c79dee755a7e688f29",
            "name": "arg_m_db_r_00003760",
        },
        # Common Main (Category 2)
        {
            "category": "3",
            "salt": 2351,
            "hash_path": "3/30/3760-2-6d0ede1abcdca14a903258993a08589b89d6ec46",
            "name": "arg_m_common_r_00003760",
        },
        # Common Title (Category 4)
        {
            "category": "3",
            "salt": 3249,
            "hash_path": "3/30/3760-2-34edbb3d724f9572ddc1dad32b7cf4e92b694c00",
            "name": "arg_t_common_r_00003760",
        },
        # iOS Main (Category 3)
        {
            "category": "2",
            "salt": 3556,
            "hash_path": "2/20/3760-2-21b54f0740a02bcf42679b2c8de45c8bfaed2b59",
            "name": "arg_m_ios_r_00003760",
        },
        # iOS Title (Category 5)
        {
            "category": "2",
            "salt": 2205,
            "hash_path": "2/20/3760-2-762b6e4b4305b395c7585d5cab7e16d840ce34ea",
            "name": "arg_t_ios_r_00003760",
        },
        # Android Main (Category 3 on Android)
        {
            "category": "1",
            "salt": 1374,
            "hash_path": "1/10/3760-2-aa1378690fd5e7f753e02c1e35e6aa1067fed074",
            "name": "arg_m_android_r_00003760",
        },
        # Android Title (Category 5 on Android)
        {
            "category": "1",
            "salt": 3034,
            "hash_path": "1/10/3760-2-425932c2f24919f8d8698f4dd76979cf21fd2676",
            "name": "arg_t_android_r_00003760",
        },
    ]

    generated: dict[str, Path] = {}
    assets_root = settings.data_dir / "assets"

    for spec in specs:
        cat_key = spec["category"]
        entries = manifest_map.get(cat_key, [])
        if not entries:
            logger.warning("No entries found for category %s (%s)", cat_key, spec["name"])
            continue

        target_file = assets_root / Path(spec["hash_path"])
        if not force and target_file.exists() and target_file.stat().st_size > 0:
            logger.info(
                "Manifest archive already exists: %s (%s bytes)",
                target_file,
                target_file.stat().st_size,
            )
            generated[spec["hash_path"]] = target_file
            continue

        target_file.parent.mkdir(parents=True, exist_ok=True)
        logger.info(
            "Generating manifest archive %s (%d entries, salt=%d)...",
            spec["name"],
            len(entries),
            spec["salt"],
        )
        payload = create_manifest_tar_gz(entries, spec["salt"])
        target_file.write_bytes(payload)
        logger.info("Saved %s -> %s (%d bytes)", spec["name"], target_file, len(payload))
        generated[spec["hash_path"]] = target_file

    return generated
