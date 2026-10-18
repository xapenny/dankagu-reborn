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

# Original stub definitions for RPC services not present in the upstream
# dankagu-local repository, synthesized for server compatibility and CI builds.
SUPPLEMENTAL_PROTOS: dict[str, str] = {
    "takasho/schema/common_featureset/player_api/restoration.proto": """syntax = "proto3";

package takasho.schema.common_featureset.player_api;

message RestorationValidateBannedWordsV1 {
  message Request {
    repeated string language_codes = 1;
    string word = 2;
  }

  message Response {
    bool contains_banned_word = 1;
    string word = 2;
  }
}

service Restoration {
  rpc ValidateBannedWordsV1(RestorationValidateBannedWordsV1.Request) returns (RestorationValidateBannedWordsV1.Response) {}
}
""",
    "takasho/schema/fes/resource/player/v1.proto": """syntax = "proto3";

package takasho.schema.fes.resource.player.v1;

import "takasho/schema/common_featureset/resource/player_storage/v2.proto";

message Player {
  repeated takasho.schema.common_featureset.resource.player_storage.v2.Entry player_storage_entries = 1;
  string nickname = 2;
  repeated string club_names = 3;
  bool is_following = 4;
  bool is_followed = 5;
  sint64 last_logged_in_at = 6;
  string player_short_id = 7;
  string player_id = 8;
}
""",
    "takasho/schema/fes/player_api/player.proto": """syntax = "proto3";

package takasho.schema.fes.player_api.player;

import "takasho/schema/common_featureset/resource/player_storage/v2.proto";
import "takasho/schema/fes/resource/player/v1.proto";

message FesPlayerGetPlayerServiceV1 {
  message Request {
    string player_id = 1;
    repeated takasho.schema.common_featureset.resource.player_storage.v2.Criterion criteria = 2;
  }

  message Response {
    takasho.schema.fes.resource.player.v1.Player player = 1;
  }
}

service FesPlayer {
  rpc GetV1(FesPlayerGetPlayerServiceV1.Request) returns (FesPlayerGetPlayerServiceV1.Response) {}
}
""",
    "takasho/schema/fes/resource/my_space/v1.proto": """syntax = "proto3";

package takasho.schema.fes.resource.my_space.v1;

import "takasho/schema/common_featureset/resource/player_storage/v2.proto";

message MySpaceLikePlayer {
  string player_id = 1;
  repeated takasho.schema.common_featureset.resource.player_storage.v2.Entry player_storage_entries = 2;
  string nickname = 3;
}

message MySpaceLike {
  MySpaceLikePlayer player = 1;
  sint64 liked_at = 2;
}
""",
    "takasho/schema/fes/player_api/my_space.proto": """syntax = "proto3";

package takasho.schema.fes.player_api;

import "takasho/schema/fes/resource/my_space/v1.proto";

message MySpaceDrawPlayerV1 {
  message Request {}

  message Response {
    string player_id = 1;
    string nickname = 2;
  }
}

message MySpaceLikeV1 {
  message Request {
    string player_id = 1;
  }

  message Response {
    string player_id = 1;
  }
}

message MySpaceGetSentLikesHistoryV1 {
  message Request {
    repeated string player_storage_keys = 1;
    string page_token = 2;
    uint64 max_results = 3;
  }

  message Response {
    repeated takasho.schema.fes.resource.my_space.v1.MySpaceLike likes = 1;
    string next_page_token = 2;
    string prev_page_token = 3;
  }
}

message MySpaceGetReceivedLikesHistoryV1 {
  message Request {
    repeated string player_storage_keys = 1;
    string page_token = 2;
    uint64 max_results = 3;
  }

  message Response {
    repeated takasho.schema.fes.resource.my_space.v1.MySpaceLike likes = 1;
    string next_page_token = 2;
    string prev_page_token = 3;
  }
}

message MySpaceGetPlayerLikeStatusV1 {
  message Request {
    string player_id = 1;
  }

  message Response {
    string player_id = 1;
    bool is_liked = 2;
  }
}

service MySpace {
  rpc DrawPlayerV1(MySpaceDrawPlayerV1.Request) returns (MySpaceDrawPlayerV1.Response) {}
  rpc LikeV1(MySpaceLikeV1.Request) returns (MySpaceLikeV1.Response) {}
  rpc GetSentLikesHistoryV1(MySpaceGetSentLikesHistoryV1.Request) returns (MySpaceGetSentLikesHistoryV1.Response) {}
  rpc GetReceivedLikesHistoryV1(MySpaceGetReceivedLikesHistoryV1.Request) returns (MySpaceGetReceivedLikesHistoryV1.Response) {}
  rpc GetPlayerLikeStatusV1(MySpaceGetPlayerLikeStatusV1.Request) returns (MySpaceGetPlayerLikeStatusV1.Response) {}
}
""",
    "takasho/schema/fes/resource/my_album/v1.proto": """syntax = "proto3";

package takasho.schema.fes.resource.my_album.v1;

import "google/protobuf/timestamp.proto";

message AlbumPhoto {
  string photo_id = 1;
  repeated string character_ids = 2;
  google.protobuf.Timestamp created_at = 3;
  string original_url = 4;
  string thumbnail_url = 5;
  string category = 6;
  bytes content_value = 7;
}
""",
    "takasho/schema/fes/player_api/my_album.proto": """syntax = "proto3";

package takasho.schema.fes.player_api;

import "google/protobuf/timestamp.proto";
import "takasho/schema/fes/resource/my_album/v1.proto";

message MyAlbumPrepareV1 {
  message Request {
    repeated string character_ids = 1;
    google.protobuf.Timestamp created_at = 2;
    bytes content_value = 3;
    string category = 4;
  }

  message Response {
    takasho.schema.fes.resource.my_album.v1.AlbumPhoto photo = 1;
    string signed_url_for_origin = 2;
    string signed_url_for_thumbnail = 3;
  }
}

message MyAlbumNotifyUploadedV1 {
  message Request {
    string photo_id = 1;
  }

  message Response {
    string photo_id = 1;
  }
}

message MyAlbumDeleteV1 {
  message Request {
    repeated string photo_ids = 1;
  }

  message Response {
    repeated string deleted_photo_ids = 1;
  }
}

message MyAlbumGetV1 {
  message Request {}

  message Response {
    repeated takasho.schema.fes.resource.my_album.v1.AlbumPhoto photos = 1;
  }
}

service MyAlbum {
  rpc PrepareV1(MyAlbumPrepareV1.Request) returns (MyAlbumPrepareV1.Response) {}
  rpc NotifyUploadedV1(MyAlbumNotifyUploadedV1.Request) returns (MyAlbumNotifyUploadedV1.Response) {}
  rpc DeleteV1(MyAlbumDeleteV1.Request) returns (MyAlbumDeleteV1.Response) {}
  rpc GetV1(MyAlbumGetV1.Request) returns (MyAlbumGetV1.Response) {}
}
""",
    "takasho/schema/fes/resource/published_photo/v1.proto": """syntax = "proto3";

package takasho.schema.fes.resource.published_photo.v1;

import "google/protobuf/timestamp.proto";

enum PrivacyType {
  PUBLIC = 0;
  PROTECTED = 1;
}

message PublishedPhoto {
  string photo_id = 1;
  repeated string character_ids = 2;
  google.protobuf.Timestamp created_at = 3;
  int64 like_count = 4;
  string category = 5;
  bytes content_value = 6;
  string original_url = 7;
  string thumbnail_url = 8;
  string player_id = 9;
}
""",
    "takasho/schema/fes/player_api/published_photo.proto": """syntax = "proto3";

package takasho.schema.fes.player_api;

import "takasho/schema/fes/resource/published_photo/v1.proto";

message PublishedPhotoPublishV1 {
  message Request {
    repeated string photo_ids = 1;
    takasho.schema.fes.resource.published_photo.v1.PrivacyType privacy_type = 2;
  }

  message Response {
    repeated takasho.schema.fes.resource.published_photo.v1.PublishedPhoto photos = 1;
  }
}

message PublishedPhotoDeleteV1 {
  message Request {
    repeated string photo_ids = 1;
  }

  message Response {
    repeated string deleted_photo_ids = 1;
  }
}

message PublishedPhotoGetVarietyV1 {
  message Request {
    int64 num_friends_photos = 2;
    int64 num_club_photos = 3;
    int64 num_latest_photos = 4;
    int64 num_latest_first_photos = 5;
    int64 num_latest_liked_photos = 6;
    int64 num_random_photos = 7;
  }

  message Response {
    repeated takasho.schema.fes.resource.published_photo.v1.PublishedPhoto friends_photos = 2;
    repeated takasho.schema.fes.resource.published_photo.v1.PublishedPhoto club_photos = 3;
    repeated takasho.schema.fes.resource.published_photo.v1.PublishedPhoto latest_photos = 4;
    repeated takasho.schema.fes.resource.published_photo.v1.PublishedPhoto latest_first_photos = 5;
    repeated takasho.schema.fes.resource.published_photo.v1.PublishedPhoto latest_liked_photos = 6;
    repeated takasho.schema.fes.resource.published_photo.v1.PublishedPhoto random_photos = 7;
  }
}

message PublishedPhotoGetByPlayerIDV1 {
  message Request {
    string player_id = 1;
  }

  message Response {
    repeated takasho.schema.fes.resource.published_photo.v1.PublishedPhoto photos = 1;
  }
}

message PublishedPhotoGetLikedV1 {
  message Request {
    string player_id = 1;
  }

  message Response {
    repeated takasho.schema.fes.resource.published_photo.v1.PublishedPhoto photos = 1;
  }
}

service PublishedPhoto {
  rpc PublishV1(PublishedPhotoPublishV1.Request) returns (PublishedPhotoPublishV1.Response) {}
  rpc DeleteV1(PublishedPhotoDeleteV1.Request) returns (PublishedPhotoDeleteV1.Response) {}
  rpc GetVarietyV1(PublishedPhotoGetVarietyV1.Request) returns (PublishedPhotoGetVarietyV1.Response) {}
  rpc GetByPlayerIDV1(PublishedPhotoGetByPlayerIDV1.Request) returns (PublishedPhotoGetByPlayerIDV1.Response) {}
  rpc GetLikedV1(PublishedPhotoGetLikedV1.Request) returns (PublishedPhotoGetLikedV1.Response) {}
}
""",
    "takasho/schema/fes/player_api/like_published_photo.proto": """syntax = "proto3";

package takasho.schema.fes.player_api;

message LikePublishedPhotoLikeV1 {
  message Request {
    string photo_id = 1;
  }

  message Response {
    string photo_id = 1;
  }
}

message LikePublishedPhotoDeleteLikeV1 {
  message Request {
    string photo_id = 1;
  }

  message Response {
    string photo_id = 1;
  }
}

service LikePublishedPhoto {
  rpc LikeV1(LikePublishedPhotoLikeV1.Request) returns (LikePublishedPhotoLikeV1.Response) {}
  rpc DeleteLikeV1(LikePublishedPhotoDeleteLikeV1.Request) returns (LikePublishedPhotoDeleteLikeV1.Response) {}
}
""",
    "takasho/schema/fes/resource/ranking/v1.proto": """syntax = "proto3";

package takasho.schema.fes.resource.ranking.v1;

message RankingEvent {
  enum Status {
    NONE = 0;
    OPEN = 1;
    CLOSE = 2;
  }
  string id = 1;
  int64 opened_at = 2;
  int64 finished_at = 3;
  int64 closed_at = 4;
  Status status = 5;
}

message PlayerRankInfo {
  string ranking_event_id = 1;
  string player_id = 2;
  int64 score = 3;
  int64 rank = 4;
  string nickname = 5;
}
""",
    "takasho/schema/fes/player_api/ranking.proto": """syntax = "proto3";

package takasho.schema.fes.player_api;

import "takasho/schema/fes/resource/ranking/v1.proto";

message RankingGetAvailableEventV1 {
  message Request {}
  message Response {
    repeated takasho.schema.fes.resource.ranking.v1.RankingEvent ranking_events = 1;
  }
}

message RankingRegisterV1 {
  message Request {
    string ranking_event_id = 1;
    int64 score = 2;
  }
  message Response {
    int64 score = 1;
  }
}

message RankingGetMyRankV1 {
  message Request {
    string ranking_event_id = 1;
  }
  message Response {
    bool is_registered = 1;
    int64 score = 2;
    int64 rank = 3;
  }
}

message RankingGetRankInfosV1 {
  message Request {
    string ranking_event_id = 1;
    int64 start = 2;
    int64 count = 3;
  }
  message Response {
    repeated takasho.schema.fes.resource.ranking.v1.PlayerRankInfo player_rank_infos = 1;
  }
}

message RankingGetRankInfosAroundMeV1 {
  message Request {
    string ranking_event_id = 1;
    int64 count = 2;
  }
  message Response {
    repeated takasho.schema.fes.resource.ranking.v1.PlayerRankInfo player_rank_infos = 1;
  }
}

message RankingGetMyCommittedRankV1 {
  message Request {
    string ranking_event_id = 1;
  }
  message Response {
    bool is_committed = 1;
    int64 score = 2;
    int64 rank = 3;
  }
}

message RankingGetTotalRegisteredCountV1 {
  message Request {
    string ranking_event_id = 1;
  }
  message Response {
    int64 count = 1;
  }
}

service Ranking {
  rpc GetAvailableEventV1(RankingGetAvailableEventV1.Request) returns (RankingGetAvailableEventV1.Response) {}
  rpc RegisterV1(RankingRegisterV1.Request) returns (RankingRegisterV1.Response) {}
  rpc GetMyRankV1(RankingGetMyRankV1.Request) returns (RankingGetMyRankV1.Response) {}
  rpc GetRankInfosV1(RankingGetRankInfosV1.Request) returns (RankingGetRankInfosV1.Response) {}
  rpc GetRankInfosAroundMeV1(RankingGetRankInfosAroundMeV1.Request) returns (RankingGetRankInfosAroundMeV1.Response) {}
  rpc GetMyCommittedRankV1(RankingGetMyCommittedRankV1.Request) returns (RankingGetMyCommittedRankV1.Response) {}
  rpc GetTotalRegisteredCount(RankingGetTotalRegisteredCountV1.Request) returns (RankingGetTotalRegisteredCountV1.Response) {}
}
""",
}


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
    for rel_path_str, proto_code in SUPPLEMENTAL_PROTOS.items():
        supp_path = PROTOS_DIR / rel_path_str
        supp_path.parent.mkdir(parents=True, exist_ok=True)
        supp_path.write_text(proto_code, encoding="utf-8")

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

    proto_include = Path(protoc.__file__).parent / "_proto"
    for proto_path in proto_files:
        rel_path = proto_path.relative_to(PROTOS_DIR)
        cmd = [
            "grpc_tools.protoc",
            f"-I{PROTOS_DIR}",
            f"-I{proto_include}",
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
            new_content = re.sub(
                r"(import\s+)(takasho\.)", r"\1dankagu.grpc.generated.\2", new_content
            )
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
