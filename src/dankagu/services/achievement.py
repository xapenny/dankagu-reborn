"""Takasho Achievement servicer for achievement rewards and progress."""

import json
import logging
import time
import uuid

import grpc
from sqlalchemy.dialects.postgresql import insert as pg_insert

from dankagu.core.database import get_sessionmaker
from dankagu.core.session import get_current_player_id
from dankagu.core.storage_state import StorageRevisionManager
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    achievement_pb2,
    achievement_pb2_grpc,
)
from dankagu.grpc.generated.takasho.schema.common_featureset.resource.player_inventory.v1_pb2 import (
    ItemType,
    Route,
)
from dankagu.models.inventory import PlayerInventoryItem
from dankagu.models.storage import PlayerStorageEntry

logger = logging.getLogger("dankagu.services.achievement")


class AchievementService(achievement_pb2_grpc.AchievementServicer):
    """Servicer for unlocking achievements and receiving prizes."""

    async def GetAvailableV1(
        self,
        request: achievement_pb2.AchievementGetAvailableV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> achievement_pb2.AchievementGetAvailableV1.Response:
        logger.info("🏆 [Achievement] GetAvailableV1")
        return achievement_pb2.AchievementGetAvailableV1.Response()

    async def GetAvailableByIDsV1(
        self,
        request: achievement_pb2.AchievementGetAvailableByIDsV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> achievement_pb2.AchievementGetAvailableByIDsV1.Response:
        logger.info("🏆 [Achievement] GetAvailableByIDsV1 (%d ids)", len(request.achievement_ids))
        response = achievement_pb2.AchievementGetAvailableByIDsV1.Response()
        for ach_id in request.achievement_ids:
            ach = response.achievements.add()
            ach.achievement_id = ach_id
            ach.opened_at = 1590000000
            ach.closed_at = 4102498799
            ach.unlock = True
        return response

    async def UnlockV1(
        self,
        request: achievement_pb2.AchievementUnlockV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> achievement_pb2.AchievementUnlockV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        logger.info(
            "🏆 [Achievement] UnlockV1 player=%s (%d ids)",
            current_pid,
            len(request.achievement_ids),
        )
        response = achievement_pb2.AchievementUnlockV1.Response()
        now = int(time.time())

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            for ach_id in request.achievement_ids:
                p_ach = response.player_achievements.add()
                p_ach.achievement.achievement_id = ach_id
                p_ach.achievement.opened_at = 1590000000
                p_ach.achievement.closed_at = 4102498799
                p_ach.achievement.unlock = True
                p_ach.last_unlocked_at = now

                # Add reward to mailbox
                item = PlayerInventoryItem(
                    id=str(uuid.uuid4()),
                    player_id=current_pid,
                    item_type=int(ItemType.VIRTUAL_CURRENCY),
                    schema_key="VC",
                    value=json.dumps({"Prefix": "VC", "Value": 90002, "Count": 100}).encode("utf-8"),
                    route=int(Route.ACHIEVEMENT),
                    message=f"実績達成報酬 ({ach_id})",
                    search_label="VC",
                    opened_at=now,
                    expired_at=now + 86400 * 90,
                    system_resource_name="FREE_STONE",
                    system_resource_num=100,
                    is_claimed=False,
                    created_at=now,
                )
                session.add(item)

                inv = response.inventories.add()
                inv.id = item.id
                inv.player_id = item.player_id
                inv.item_type = item.item_type
                inv.schema_key = item.schema_key
                inv.value = item.value
                inv.route = item.route
                inv.message = item.message
                inv.search_label = item.search_label
                inv.opened_at = item.opened_at
                inv.expired_at = item.expired_at
                inv.system_resource_name = item.system_resource_name
                inv.system_resource_num = item.system_resource_num
                inv.created_at = item.created_at

            await session.commit()

        return response

    async def UnlockAndSavePlayerStorageV1(
        self,
        request: achievement_pb2.AchievementUnlockAndSavePlayerStorageV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> achievement_pb2.AchievementUnlockAndSavePlayerStorageV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        logger.info(
            "🏆 [Achievement] UnlockAndSavePlayerStorageV1 player=%s (%d achs, %d entries)",
            current_pid,
            len(request.achievement_ids),
            len(request.entries),
        )
        response = achievement_pb2.AchievementUnlockAndSavePlayerStorageV1.Response()
        now = int(time.time())

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            # 1. Save storage entries
            for proto_entry in request.entries:
                upsert_stmt = pg_insert(PlayerStorageEntry).values(
                    player_id=current_pid,
                    key=proto_entry.key,
                    value=proto_entry.value,
                    created_at=proto_entry.created_at or now,
                    updated_at=now,
                )
                upsert_stmt = upsert_stmt.on_conflict_do_update(
                    index_elements=[PlayerStorageEntry.player_id, PlayerStorageEntry.key],
                    set_={
                        "value": upsert_stmt.excluded.value,
                        "updated_at": upsert_stmt.excluded.updated_at,
                    },
                )
                await session.execute(upsert_stmt)

                resp_entry = response.entries.add()
                resp_entry.player_id = current_pid
                resp_entry.key = proto_entry.key
                resp_entry.value = proto_entry.value
                resp_entry.created_at = proto_entry.created_at or now
                resp_entry.updated_at = now

            # 2. Unlock achievements and grant rewards to mailbox
            for ach_id in request.achievement_ids:
                p_ach = response.player_achievements.add()
                p_ach.achievement.achievement_id = ach_id
                p_ach.achievement.opened_at = 1590000000
                p_ach.achievement.closed_at = 4102498799
                p_ach.achievement.unlock = True
                p_ach.last_unlocked_at = now

                item = PlayerInventoryItem(
                    id=str(uuid.uuid4()),
                    player_id=current_pid,
                    item_type=int(ItemType.VIRTUAL_CURRENCY),
                    schema_key="VC",
                    value=json.dumps({"Prefix": "VC", "Value": 90002, "Count": 100}).encode("utf-8"),
                    route=int(Route.ACHIEVEMENT),
                    message=f"実績達成報酬 ({ach_id})",
                    search_label="VC",
                    opened_at=now,
                    expired_at=now + 86400 * 90,
                    system_resource_name="FREE_STONE",
                    system_resource_num=100,
                    is_claimed=False,
                    created_at=now,
                )
                session.add(item)

                inv = response.inventories.add()
                inv.id = item.id
                inv.player_id = item.player_id
                inv.item_type = item.item_type
                inv.schema_key = item.schema_key
                inv.value = item.value
                inv.route = item.route
                inv.message = item.message
                inv.search_label = item.search_label
                inv.opened_at = item.opened_at
                inv.expired_at = item.expired_at
                inv.system_resource_name = item.system_resource_name
                inv.system_resource_num = item.system_resource_num
                inv.created_at = item.created_at

            await session.commit()

        response.revision = await StorageRevisionManager.set_revision_and_save(
            request.next_revision, player_id=current_pid
        )
        return response


def register_achievement_servicer(server: grpc.aio.Server) -> None:
    servicer = AchievementService()
    method_handlers = {
        "GetAvailableV1": takasho_unary_handler(
            servicer.GetAvailableV1,
            achievement_pb2.AchievementGetAvailableV1.Request,
            achievement_pb2.AchievementGetAvailableV1.Response,
        ),
        "GetAvailableByIDsV1": takasho_unary_handler(
            servicer.GetAvailableByIDsV1,
            achievement_pb2.AchievementGetAvailableByIDsV1.Request,
            achievement_pb2.AchievementGetAvailableByIDsV1.Response,
        ),
        "UnlockV1": takasho_unary_handler(
            servicer.UnlockV1,
            achievement_pb2.AchievementUnlockV1.Request,
            achievement_pb2.AchievementUnlockV1.Response,
        ),
        "UnlockAndSavePlayerStorageV1": takasho_unary_handler(
            servicer.UnlockAndSavePlayerStorageV1,
            achievement_pb2.AchievementUnlockAndSavePlayerStorageV1.Request,
            achievement_pb2.AchievementUnlockAndSavePlayerStorageV1.Response,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.common_featureset.player_api.Achievement", method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))
