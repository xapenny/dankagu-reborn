"""Takasho System servicer for authorization and player status."""

import logging
import time
import uuid

import grpc
from sqlalchemy import select

from dankagu.core.database import get_sessionmaker
from dankagu.core.growth import create_daily_reward_mail_items
from dankagu.core.identity import (
    is_onboarded,
    normalise_device_account,
    resolve_or_create_player,
)
from dankagu.core.session import create_session, get_current_player_id, set_current_player
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    system_pb2,
    system_pb2_grpc,
)
from dankagu.lcx.tokens import decode_token_user_id
from dankagu.models.player import Player
from dankagu.models.transfer import AccountTransfer

logger = logging.getLogger("dankagu.services.system")


def get_jst_game_day(epoch_seconds: int) -> int:
    """Calculate the integer day index under JST (UTC+9) 04:00 daily boundary.

    Shift: +9 hours (JST) - 4 hours (daily reset at 04:00 JST) = +5 hours (18,000s).
    """
    return (epoch_seconds + 5 * 3600) // 86400


def process_player_login(session, player: Player, now: int) -> bool:
    """Update player login time and advance total_login_days if crossing 04:00 JST.

    Returns True if a new daily login was recorded and rewarded.
    """
    day_last = get_jst_game_day(player.last_login_at)
    day_now = get_jst_game_day(now)
    is_new_day = day_now > day_last
    if is_new_day:
        player.total_login_days += 1
        mail_items = create_daily_reward_mail_items(
            player_id=player.id,
            total_login_days=player.total_login_days,
        )
        session.add_all(mail_items)

        logger.info(
            "🎁 Player %s logged in on new JST game day (total_login_days=%d, %d daily reward mails queued)",
            player.id,
            player.total_login_days,
            len(mail_items),
        )
    player.last_login_at = now
    return is_new_day


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
            # Identity rules are shared with the LCX surface so both entry points
            # resolve the same device to the same player (see core.identity).
            player, matched_by = await resolve_or_create_player(
                session,
                user_id=user_id,
                device_account=device_account,
                allow_onboarded_device_match=bool(user_id),
            )

            if matched_by != "created":
                process_player_login(session, player, now)
                usable_device = normalise_device_account(device_account)
                if usable_device and player.device_account != usable_device:
                    former = (
                        (
                            await session.execute(
                                select(Player).where(
                                    Player.device_account == usable_device,
                                    Player.id != player.id,
                                )
                            )
                        )
                        .scalars()
                        .first()
                    )
                    if former is None:
                        player.device_account = usable_device
                        logger.info(
                            "🔗 [AuthorizeV3] bound device %r to player %s",
                            usable_device,
                            player.id,
                        )
                    else:
                        has_transfer = (
                            await session.execute(
                                select(AccountTransfer).where(
                                    AccountTransfer.player_id == player.id
                                )
                            )
                        ).scalars().first() is not None
                        if not is_onboarded(former) or has_transfer:
                            former.device_account = None
                            player.device_account = usable_device
                            logger.info(
                                "🔗 [AuthorizeV3] transferred device %r from former player %s to player %s",
                                usable_device,
                                former.id,
                                player.id,
                            )
                        else:
                            logger.warning(
                                "🔒 [AuthorizeV3] device %r is already bound to onboarded player %s; "
                                "keeping player %s unbound to prevent device collision",
                                usable_device,
                                former.id,
                                player.id,
                            )

            player_id = player.id
            p_user_id = player.user_id
            # Generate persistent session token within the same DB transaction
            session_token = await create_session(
                player_id=player_id, user_id=p_user_id, session=session
            )
            await session.commit()

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
        status_entry.description = "NORMAL"
        status_entry.expired_at = 4102444800
        status_entry.has_expired_at = False

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
                process_player_login(session, player, now)
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
