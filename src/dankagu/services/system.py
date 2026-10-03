"""Takasho System servicer for authorization and player status."""

import logging
import time
import uuid

import grpc
from sqlalchemy import select

from dankagu.core.database import get_sessionmaker
from dankagu.core.session import create_session, get_current_player_id, set_current_player
from dankagu.core.starter import seed_starter_player
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    system_pb2,
    system_pb2_grpc,
)
from dankagu.lcx.tokens import decode_token_user_id
from dankagu.models.player import Player

logger = logging.getLogger("dankagu.services.system")


class SystemService(system_pb2_grpc.SystemServicer):
    """Handles System.Authorize, GetPlayerStatus, and login records."""

    async def AuthorizeV3(
        self,
        request: system_pb2.SystemAuthorizeV3.Request,
        context: grpc.aio.ServicerContext,
    ) -> system_pb2.SystemAuthorizeV3.Response:
        now = int(time.time())
        user_id: str | None = None
        if request.id_token:
            user_id = decode_token_user_id(request.id_token)

        device_account: str | None = request.device_account or None

        if not user_id and not device_account:
            logger.warning(
                "🔒 [AuthorizeV3] Rejected: neither id_token nor device_account provided"
            )
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "Missing user identity or device account"
            )
            return system_pb2.SystemAuthorizeV3.Response()

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            player: Player | None = None
            if user_id:
                stmt = select(Player).where(Player.user_id == user_id)
                res = await session.execute(stmt)
                player = res.scalars().first()

            if player is None and device_account:
                stmt = select(Player).where(Player.device_account == device_account)
                res = await session.execute(stmt)
                player = res.scalars().first()

            if player is None:
                new_user_id = user_id or str(uuid.uuid4())
                new_dev = device_account or str(uuid.uuid4())
                player = Player(
                    user_id=new_user_id,
                    device_account=new_dev,
                    registered_at=now,
                    last_login_at=now,
                    total_login_days=1,
                )
                session.add(player)
                await session.flush()

                # Seed starter wallet and player storage entries
                await seed_starter_player(session, player)
                await session.commit()
                await session.refresh(player)
            else:
                player.last_login_at = now
                if device_account and not player.device_account:
                    player.device_account = device_account
                await session.commit()

            player_id = player.id
            p_user_id = player.user_id

        # Generate persistent session token
        session_token = await create_session(player_id=player_id, user_id=p_user_id)
        set_current_player(player_id, session_token)

        response = system_pb2.SystemAuthorizeV3.Response()
        response.session_token = session_token
        response.player_id = player_id
        return response

    async def AuthorizeV2(
        self,
        request: system_pb2.SystemAuthorizeV2.Request,
        context: grpc.aio.ServicerContext,
    ) -> system_pb2.SystemAuthorizeV2.Response:
        v3_req = system_pb2.SystemAuthorizeV3.Request(
            device_account=request.device_account,
            device_password=request.device_password,
            device_info=request.device_info,
        )
        v3_res = await self.AuthorizeV3(v3_req, context)
        response = system_pb2.SystemAuthorizeV2.Response()
        response.session_token = v3_res.session_token
        response.player_id = v3_res.player_id
        return response

    async def GetPlayerStatusV2(
        self,
        request: system_pb2.SystemGetPlayerStatusV2.Request,
        context: grpc.aio.ServicerContext,
    ) -> system_pb2.SystemGetPlayerStatusV2.Response:
        current_pid = get_current_player_id()
        if not current_pid:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Player session required")
            return system_pb2.SystemGetPlayerStatusV2.Response()

        response = system_pb2.SystemGetPlayerStatusV2.Response()
        # Normal active status
        status_entry = response.player_statuses.add()
        status_entry.status = 1
        status_entry.effective_from = 0
        status_entry.effective_to = 4102444800

        total_login_days = 1
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            player = await session.get(Player, current_pid)
            if player:
                total_login_days = player.total_login_days

        response.total_login_days = total_login_days
        return response

    async def RecordLoginHistoryV1(
        self,
        request: system_pb2.SystemRecordLoginHistoryV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> system_pb2.SystemRecordLoginHistoryV1.Response:
        current_pid = get_current_player_id()
        if not current_pid:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Player session required")
            return system_pb2.SystemRecordLoginHistoryV1.Response()

        now = int(time.time())
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            player = await session.get(Player, current_pid)
            if player:
                player.last_login_at = now
                await session.commit()

        response = system_pb2.SystemRecordLoginHistoryV1.Response()
        response.logged_in_at = now
        return response

    async def GetWebTokenV1(
        self,
        request: system_pb2.SystemGetWebTokenV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> system_pb2.SystemGetWebTokenV1.Response:
        response = system_pb2.SystemGetWebTokenV1.Response()
        response.id_token = str(uuid.uuid4())
        return response


def register_system_servicer(server: grpc.aio.Server) -> None:
    servicer = SystemService()
    method_handlers = {
        "AuthorizeV3": takasho_unary_handler(
            servicer.AuthorizeV3,
            system_pb2.SystemAuthorizeV3.Request,
            system_pb2.SystemAuthorizeV3.Response,
        ),
        "AuthorizeV2": takasho_unary_handler(
            servicer.AuthorizeV2,
            system_pb2.SystemAuthorizeV2.Request,
            system_pb2.SystemAuthorizeV2.Response,
        ),
        "GetPlayerStatusV2": takasho_unary_handler(
            servicer.GetPlayerStatusV2,
            system_pb2.SystemGetPlayerStatusV2.Request,
            system_pb2.SystemGetPlayerStatusV2.Response,
        ),
        "RecordLoginHistoryV1": takasho_unary_handler(
            servicer.RecordLoginHistoryV1,
            system_pb2.SystemRecordLoginHistoryV1.Request,
            system_pb2.SystemRecordLoginHistoryV1.Response,
        ),
        "GetWebTokenV1": takasho_unary_handler(
            servicer.GetWebTokenV1,
            system_pb2.SystemGetWebTokenV1.Request,
            system_pb2.SystemGetWebTokenV1.Response,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.common_featureset.player_api.System", method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))
