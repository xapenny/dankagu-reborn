"""Compile Takasho protobuf schemas into ``src/dankagu/grpc/generated/``.

The ``.proto`` definitions are third-party content and are intentionally **not**
redistributed with this repository. Fetch them once with ``--fetch``, or drop a
copy into ``protos/`` yourself.

Usage::

    python scripts/compile_protos.py --fetch   # download schemas, then compile
    python scripts/compile_protos.py           # compile what is in protos/

Source of the schemas: https://github.com/RainbowUnicorn7297/dankagu-local
(MIT licensed — see THIRD_PARTY_NOTICES.md.)
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from grpc_tools import protoc

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROTOS_DIR = PROJECT_ROOT / "protos"
OUTPUT_DIR = PROJECT_ROOT / "src" / "dankagu" / "grpc" / "generated"
VENDOR_DIR = PROJECT_ROOT / "vendor-protos"

UPSTREAM_REPO = "https://github.com/RainbowUnicorn7297/dankagu-local.git"
UPSTREAM_SUBDIR = "protos"


def fetch_protos() -> int:
    """Sparse-clone the upstream repository and copy its ``protos/`` tree here."""
    if shutil.which("git") is None:
        print("error: git is required for --fetch", file=sys.stderr)
        return 1

    if VENDOR_DIR.exists():
        shutil.rmtree(VENDOR_DIR)

    print(f"Fetching protocol schemas from {UPSTREAM_REPO} ...")
    steps = [
        ["clone", "--depth", "1", "--filter=blob:none", "--sparse", UPSTREAM_REPO, str(VENDOR_DIR)],
        ["-C", str(VENDOR_DIR), "sparse-checkout", "set", UPSTREAM_SUBDIR],
    ]
    for step in steps:
        result = subprocess.run(["git", *step], capture_output=True, text=True)
        if result.returncode != 0:
            print(f"error: git {' '.join(step)} failed:\n{result.stderr}", file=sys.stderr)
            return result.returncode

    src = VENDOR_DIR / UPSTREAM_SUBDIR
    if not src.is_dir():
        print(f"error: {UPSTREAM_SUBDIR}/ not found in upstream checkout", file=sys.stderr)
        return 1

    PROTOS_DIR.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = PROTOS_DIR / item.name
        if target.exists():
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        shutil.copytree(item, target) if item.is_dir() else shutil.copy2(item, target)

    count = len(list(PROTOS_DIR.rglob("*.proto")))
    print(f"Copied {count} .proto files into {PROTOS_DIR}")
    return 0


def compile_all_protos() -> int:
    """Compile every ``.proto`` under ``protos/`` and rewrite imports."""
    proto_files = sorted(PROTOS_DIR.rglob("*.proto")) if PROTOS_DIR.is_dir() else []
    if not proto_files:
        print(
            "error: no .proto files found in "
            f"{PROTOS_DIR}\n\n"
            "The protocol schemas are third-party content and are not distributed\n"
            "with this repository. Fetch them first:\n\n"
            "    python scripts/compile_protos.py --fetch\n\n"
            f"Upstream source: {UPSTREAM_REPO} (MIT, see THIRD_PARTY_NOTICES.md)\n",
            file=sys.stderr,
        )
        return 1

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Found {len(proto_files)} .proto files in {PROTOS_DIR}")

    for proto_path in proto_files:
        rel_path = proto_path.relative_to(PROTOS_DIR)
        cmd = [
            "grpc_tools.protoc",
            f"-I{PROTOS_DIR}",
            f"--python_out={OUTPUT_DIR}",
            f"--pyi_out={OUTPUT_DIR}",
            f"--grpc_python_out={OUTPUT_DIR}",
            rel_path.as_posix(),
        ]
        ret = protoc.main(cmd)
        if ret != 0:
            print(f"Error compiling {rel_path}: code {ret}", file=sys.stderr)
            return ret

    print("Compiled. Creating __init__.py files...")
    for root, _dirs, _files in os.walk(OUTPUT_DIR):
        init_file = Path(root) / "__init__.py"
        if not init_file.exists():
            init_file.write_text("# Auto-generated\n", encoding="utf-8")

    print("Fixing relative imports...")
    # grpc_tools emits `from takasho.schema... import ...`; rewrite so the
    # stubs are importable as dankagu.grpc.generated.takasho.schema...
    for root, _dirs, files in os.walk(OUTPUT_DIR):
        for file_name in files:
            if not (file_name.endswith(".py") or file_name.endswith(".pyi")):
                continue
            file_path = Path(root) / file_name
            content = file_path.read_text(encoding="utf-8")
            new_content = re.sub(r"(from\s+)(takasho\.)", r"\1dankagu.grpc.generated.\2", content)
            new_content = re.sub(r"(import\s+)(takasho\.)", r"\1dankagu.grpc.generated.\2", new_content)
            if new_content != content:
                file_path.write_text(new_content, encoding="utf-8")

    print("Protobuf compilation and import fixing completed successfully!")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="download the protocol schemas from the upstream project before compiling",
    )
    args = parser.parse_args()

    if args.fetch:
        ret = fetch_protos()
        if ret != 0:
            return ret

    return compile_all_protos()


if __name__ == "__main__":
    sys.exit(main())
