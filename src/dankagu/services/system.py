"""Takasho System servicer for authorization and player status."""

import time
import uuid

import grpc
from sqlalchemy import select

from dankagu.core.database import get_sessionmaker
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    system_pb2,
    system_pb2_grpc,
)
from dankagu.models.player import Player


class SystemService(system_pb2_grpc.SystemServicer):
    """Handles System.Authorize, GetPlayerStatus, and login records."""

    async def AuthorizeV3(
        self,
        request: system_pb2.SystemAuthorizeV3.Request,
        context: grpc.aio.ServicerContext,
    ) -> system_pb2.SystemAuthorizeV3.Response:
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = select(Player)
            if request.device_account:
                stmt = stmt.where(Player.device_account == request.device_account)
            result = await session.execute(stmt.limit(1))
            player = result.scalars().first()

            now = int(time.time())
            if player is None:
                user_id = str(uuid.uuid4())
                player = Player(
                    user_id=user_id,
                    device_account=request.device_account or str(uuid.uuid4()),
                    registered_at=now,
                    last_login_at=now,
                )
                session.add(player)
                await session.commit()
                await session.refresh(player)
            else:
                player.last_login_at = now
                await session.commit()

            player_id = player.id
            session_token = str(uuid.uuid4())

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
        response = system_pb2.SystemGetPlayerStatusV2.Response()
        # Normal active status
        status_entry = response.player_statuses.add()
        status_entry.status = 1
        status_entry.effective_from = 0
        status_entry.effective_to = 4102444800

        response.total_login_days = 1
        return response

    async def RecordLoginHistoryV1(
        self,
        request: system_pb2.SystemRecordLoginHistoryV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> system_pb2.SystemRecordLoginHistoryV1.Response:
        response = system_pb2.SystemRecordLoginHistoryV1.Response()
        response.logged_in_at = int(time.time())
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
