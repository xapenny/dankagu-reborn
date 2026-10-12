"""Takasho MyAlbum, PublishedPhoto, and LikePublishedPhoto servicers."""

import logging
import random
import time
import uuid

import grpc
from sqlalchemy import delete, func, select

from dankagu.core.database import get_sessionmaker
from dankagu.core.session import get_current_player_id
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.fes.player_api import (
    like_published_photo_pb2,
    like_published_photo_pb2_grpc,
    my_album_pb2,
    my_album_pb2_grpc,
    published_photo_pb2,
    published_photo_pb2_grpc,
)
from dankagu.grpc.generated.takasho.schema.fes.resource.my_album import (
    v1_pb2 as my_album_v1_pb2,
)
from dankagu.grpc.generated.takasho.schema.fes.resource.published_photo import (
    v1_pb2 as published_photo_v1_pb2,
)
from dankagu.models.social import FriendRelation, PhotoLikeRecord, PlayerPhoto

logger = logging.getLogger("dankagu.services.photo_album")


def _fill_album_photo(proto: my_album_v1_pb2.AlbumPhoto, row: PlayerPhoto) -> None:
    proto.photo_id = row.photo_id
    if row.character_ids:
        proto.character_ids.extend(str(cid) for cid in row.character_ids)
    proto.created_at.seconds = int(row.created_at)
    proto.created_at.nanos = 0
    proto.original_url = row.original_url
    proto.thumbnail_url = row.thumbnail_url
    proto.category = row.category
    proto.content_value = row.content_value or b""


def _fill_published_photo(
    proto: published_photo_v1_pb2.PublishedPhoto, row: PlayerPhoto
) -> None:
    proto.photo_id = row.photo_id
    if row.character_ids:
        proto.character_ids.extend(str(cid) for cid in row.character_ids)
    proto.created_at.seconds = int(row.created_at)
    proto.created_at.nanos = 0
    proto.like_count = int(row.like_count)
    proto.category = row.category
    proto.content_value = row.content_value or b""
    proto.original_url = row.original_url
    proto.thumbnail_url = row.thumbnail_url
    proto.player_id = row.player_id


class MyAlbumService(my_album_pb2_grpc.MyAlbumServicer):
    """Servicer for personal photo album management."""

    async def PrepareV1(
        self,
        request: my_album_pb2.MyAlbumPrepareV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> my_album_pb2.MyAlbumPrepareV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        now = int(time.time())
        created_sec = (
            int(request.created_at.seconds)
            if request.HasField("created_at") and request.created_at.seconds > 0
            else now
        )
        photo_id = str(uuid.uuid4())
        orig_url = f"https://cdn.assets.local/photos/{photo_id}_orig.png"
        thumb_url = f"https://cdn.assets.local/photos/{photo_id}_thumb.png"

        logger.info(
            "📸 [MyAlbum] PrepareV1 (player=%s, photo_id=%s, category=%s)",
            current_pid,
            photo_id,
            request.category,
        )

        row = PlayerPhoto(
            photo_id=photo_id,
            player_id=current_pid,
            character_ids=list(request.character_ids),
            category=request.category or "",
            content_value=bytes(request.content_value),
            original_url=orig_url,
            thumbnail_url=thumb_url,
            is_uploaded=False,
            is_published=False,
            privacy_type=0,
            like_count=0,
            created_at=created_sec,
            published_at=0,
        )

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            session.add(row)
            await session.commit()

        response = my_album_pb2.MyAlbumPrepareV1.Response()
        _fill_album_photo(response.photo, row)
        response.signed_url_for_origin = orig_url
        response.signed_url_for_thumbnail = thumb_url
        return response

    async def NotifyUploadedV1(
        self,
        request: my_album_pb2.MyAlbumNotifyUploadedV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> my_album_pb2.MyAlbumNotifyUploadedV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        logger.info(
            "📸 [MyAlbum] NotifyUploadedV1 (player=%s, photo_id=%s)",
            current_pid,
            request.photo_id,
        )
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            row = await session.get(PlayerPhoto, request.photo_id)
            if row and row.player_id == current_pid:
                row.is_uploaded = True
                await session.commit()

        response = my_album_pb2.MyAlbumNotifyUploadedV1.Response()
        response.photo_id = request.photo_id
        return response

    async def DeleteV1(
        self,
        request: my_album_pb2.MyAlbumDeleteV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> my_album_pb2.MyAlbumDeleteV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        target_ids = list(request.photo_ids)
        logger.info(
            "📸 [MyAlbum] DeleteV1 (player=%s, count=%d)",
            current_pid,
            len(target_ids),
        )
        response = my_album_pb2.MyAlbumDeleteV1.Response()
        if not target_ids:
            return response

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            res = await session.execute(
                select(PlayerPhoto.photo_id).where(
                    PlayerPhoto.player_id == current_pid,
                    PlayerPhoto.photo_id.in_(target_ids),
                )
            )
            owned_ids = list(res.scalars().all())
            if owned_ids:
                await session.execute(
                    delete(PhotoLikeRecord).where(PhotoLikeRecord.photo_id.in_(owned_ids))
                )
                await session.execute(
                    delete(PlayerPhoto).where(PlayerPhoto.photo_id.in_(owned_ids))
                )
                await session.commit()
                response.deleted_photo_ids.extend(owned_ids)

        return response

    async def GetV1(
        self,
        request: my_album_pb2.MyAlbumGetV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> my_album_pb2.MyAlbumGetV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        logger.info("📸 [MyAlbum] GetV1 (player=%s)", current_pid)
        response = my_album_pb2.MyAlbumGetV1.Response()

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = (
                select(PlayerPhoto)
                .where(PlayerPhoto.player_id == current_pid)
                .order_by(PlayerPhoto.created_at.desc())
            )
            rows = list((await session.execute(stmt)).scalars().all())
            for r in rows:
                p = response.photos.add()
                _fill_album_photo(p, r)

        return response


class PublishedPhotoService(published_photo_pb2_grpc.PublishedPhotoServicer):
    """Servicer for public photo exhibition gallery."""

    async def PublishV1(
        self,
        request: published_photo_pb2.PublishedPhotoPublishV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> published_photo_pb2.PublishedPhotoPublishV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        target_ids = list(request.photo_ids)
        now = int(time.time())
        logger.info(
            "🖼️ [PublishedPhoto] PublishV1 (player=%s, count=%d, privacy=%d)",
            current_pid,
            len(target_ids),
            int(request.privacy_type),
        )
        response = published_photo_pb2.PublishedPhotoPublishV1.Response()
        if not target_ids:
            return response

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = select(PlayerPhoto).where(
                PlayerPhoto.player_id == current_pid,
                PlayerPhoto.photo_id.in_(target_ids),
            )
            rows = list((await session.execute(stmt)).scalars().all())
            for r in rows:
                r.is_published = True
                r.privacy_type = int(request.privacy_type)
                r.published_at = now
            await session.commit()

            for r in rows:
                p = response.photos.add()
                _fill_published_photo(p, r)

        return response

    async def DeleteV1(
        self,
        request: published_photo_pb2.PublishedPhotoDeleteV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> published_photo_pb2.PublishedPhotoDeleteV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        target_ids = list(request.photo_ids)
        logger.info(
            "🖼️ [PublishedPhoto] DeleteV1 (player=%s, count=%d)",
            current_pid,
            len(target_ids),
        )
        response = published_photo_pb2.PublishedPhotoDeleteV1.Response()
        if not target_ids:
            return response

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = select(PlayerPhoto).where(
                PlayerPhoto.player_id == current_pid,
                PlayerPhoto.photo_id.in_(target_ids),
                PlayerPhoto.is_published.is_(True),
            )
            rows = list((await session.execute(stmt)).scalars().all())
            for r in rows:
                r.is_published = False
                response.deleted_photo_ids.append(r.photo_id)
            await session.commit()

        return response

    async def GetVarietyV1(
        self,
        request: published_photo_pb2.PublishedPhotoGetVarietyV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> published_photo_pb2.PublishedPhotoGetVarietyV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        logger.info("🖼️ [PublishedPhoto] GetVarietyV1 (player=%s)", current_pid)
        response = published_photo_pb2.PublishedPhotoGetVarietyV1.Response()

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            # 1. Friends' photos
            if request.num_friends_photos > 0:
                follow_res = await session.execute(
                    select(FriendRelation.target_id).where(
                        FriendRelation.follower_id == current_pid
                    )
                )
                friend_ids = list(follow_res.scalars().all())
                if friend_ids:
                    f_stmt = (
                        select(PlayerPhoto)
                        .where(
                            PlayerPhoto.is_published.is_(True),
                            PlayerPhoto.player_id.in_(friend_ids),
                        )
                        .order_by(PlayerPhoto.published_at.desc())
                        .limit(int(request.num_friends_photos))
                    )
                    for r in (await session.execute(f_stmt)).scalars().all():
                        _fill_published_photo(response.friends_photos.add(), r)

            # 2. Latest public photos
            n_latest = int(request.num_latest_photos) if request.num_latest_photos > 0 else 10
            l_stmt = (
                select(PlayerPhoto)
                .where(PlayerPhoto.is_published.is_(True))
                .order_by(PlayerPhoto.published_at.desc())
                .limit(n_latest)
            )
            latest_rows = list((await session.execute(l_stmt)).scalars().all())
            for r in latest_rows:
                _fill_published_photo(response.latest_photos.add(), r)

            # 3. Latest first-like candidate photos (like_count == 0)
            if request.num_latest_first_photos > 0:
                lf_stmt = (
                    select(PlayerPhoto)
                    .where(
                        PlayerPhoto.is_published.is_(True),
                        PlayerPhoto.like_count == 0,
                    )
                    .order_by(PlayerPhoto.published_at.desc())
                    .limit(int(request.num_latest_first_photos))
                )
                for r in (await session.execute(lf_stmt)).scalars().all():
                    _fill_published_photo(response.latest_first_photos.add(), r)

            # 4. Popular / Liked photos
            if request.num_latest_liked_photos > 0:
                ll_stmt = (
                    select(PlayerPhoto)
                    .where(
                        PlayerPhoto.is_published.is_(True),
                        PlayerPhoto.like_count > 0,
                    )
                    .order_by(PlayerPhoto.like_count.desc(), PlayerPhoto.published_at.desc())
                    .limit(int(request.num_latest_liked_photos))
                )
                for r in (await session.execute(ll_stmt)).scalars().all():
                    _fill_published_photo(response.latest_liked_photos.add(), r)

            # 5. Random photos
            if request.num_random_photos > 0 and latest_rows:
                sample_k = min(len(latest_rows), int(request.num_random_photos))
                for r in random.sample(latest_rows, sample_k):
                    _fill_published_photo(response.random_photos.add(), r)

        return response

    async def GetByPlayerIDV1(
        self,
        request: published_photo_pb2.PublishedPhotoGetByPlayerIDV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> published_photo_pb2.PublishedPhotoGetByPlayerIDV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        target_pid = request.player_id or current_pid
        logger.info("🖼️ [PublishedPhoto] GetByPlayerIDV1 (target=%s)", target_pid)
        response = published_photo_pb2.PublishedPhotoGetByPlayerIDV1.Response()

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = (
                select(PlayerPhoto)
                .where(
                    PlayerPhoto.player_id == target_pid,
                    PlayerPhoto.is_published.is_(True),
                )
                .order_by(PlayerPhoto.published_at.desc())
            )
            for r in (await session.execute(stmt)).scalars().all():
                _fill_published_photo(response.photos.add(), r)

        return response

    async def GetLikedV1(
        self,
        request: published_photo_pb2.PublishedPhotoGetLikedV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> published_photo_pb2.PublishedPhotoGetLikedV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        target_pid = request.player_id or current_pid
        logger.info("🖼️ [PublishedPhoto] GetLikedV1 (player=%s)", target_pid)
        response = published_photo_pb2.PublishedPhotoGetLikedV1.Response()

        session_factory = get_sessionmaker()
        async with session_factory() as session:
            stmt = (
                select(PlayerPhoto)
                .join(PhotoLikeRecord, PhotoLikeRecord.photo_id == PlayerPhoto.photo_id)
                .where(
                    PhotoLikeRecord.player_id == target_pid,
                    PlayerPhoto.is_published.is_(True),
                )
                .order_by(PhotoLikeRecord.created_at.desc())
            )
            for r in (await session.execute(stmt)).scalars().all():
                _fill_published_photo(response.photos.add(), r)

        return response


class LikePublishedPhotoService(like_published_photo_pb2_grpc.LikePublishedPhotoServicer):
    """Servicer for liking and unliking published gallery photos."""

    async def LikeV1(
        self,
        request: like_published_photo_pb2.LikePublishedPhotoLikeV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> like_published_photo_pb2.LikePublishedPhotoLikeV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        photo_id = request.photo_id
        now = int(time.time())
        logger.info("❤️ [LikePublishedPhoto] LikeV1 (player=%s, photo_id=%s)", current_pid, photo_id)

        if photo_id:
            session_factory = get_sessionmaker()
            async with session_factory() as session:
                existing = (
                    await session.execute(
                        select(PhotoLikeRecord).where(
                            PhotoLikeRecord.photo_id == photo_id,
                            PhotoLikeRecord.player_id == current_pid,
                        )
                    )
                ).scalars().first()
                if existing is None:
                    session.add(
                        PhotoLikeRecord(
                            photo_id=photo_id,
                            player_id=current_pid,
                            created_at=now,
                        )
                    )
                    await session.flush()

                photo = await session.get(PlayerPhoto, photo_id)
                if photo:
                    cnt = (
                        await session.execute(
                            select(func.count())
                            .select_from(PhotoLikeRecord)
                            .where(PhotoLikeRecord.photo_id == photo_id)
                        )
                    ).scalar() or 0
                    photo.like_count = int(cnt)

                await session.commit()

        response = like_published_photo_pb2.LikePublishedPhotoLikeV1.Response()
        response.photo_id = photo_id
        return response

    async def DeleteLikeV1(
        self,
        request: like_published_photo_pb2.LikePublishedPhotoDeleteLikeV1.Request,
        context: grpc.aio.ServicerContext,
    ) -> like_published_photo_pb2.LikePublishedPhotoDeleteLikeV1.Response:
        current_pid = get_current_player_id() or "dankagu-reborn-user"
        photo_id = request.photo_id
        logger.info(
            "💔 [LikePublishedPhoto] DeleteLikeV1 (player=%s, photo_id=%s)",
            current_pid,
            photo_id,
        )

        if photo_id:
            session_factory = get_sessionmaker()
            async with session_factory() as session:
                await session.execute(
                    delete(PhotoLikeRecord).where(
                        PhotoLikeRecord.photo_id == photo_id,
                        PhotoLikeRecord.player_id == current_pid,
                    )
                )
                await session.flush()

                photo = await session.get(PlayerPhoto, photo_id)
                if photo:
                    cnt = (
                        await session.execute(
                            select(func.count())
                            .select_from(PhotoLikeRecord)
                            .where(PhotoLikeRecord.photo_id == photo_id)
                        )
                    ).scalar() or 0
                    photo.like_count = int(cnt)

                await session.commit()

        response = like_published_photo_pb2.LikePublishedPhotoDeleteLikeV1.Response()
        response.photo_id = photo_id
        return response


def register_photo_album_servicers(server: grpc.aio.Server) -> None:
    album_servicer = MyAlbumService()
    album_handlers = {
        "PrepareV1": takasho_unary_handler(
            album_servicer.PrepareV1,
            my_album_pb2.MyAlbumPrepareV1.Request,
            my_album_pb2.MyAlbumPrepareV1.Response,
        ),
        "NotifyUploadedV1": takasho_unary_handler(
            album_servicer.NotifyUploadedV1,
            my_album_pb2.MyAlbumNotifyUploadedV1.Request,
            my_album_pb2.MyAlbumNotifyUploadedV1.Response,
        ),
        "DeleteV1": takasho_unary_handler(
            album_servicer.DeleteV1,
            my_album_pb2.MyAlbumDeleteV1.Request,
            my_album_pb2.MyAlbumDeleteV1.Response,
        ),
        "GetV1": takasho_unary_handler(
            album_servicer.GetV1,
            my_album_pb2.MyAlbumGetV1.Request,
            my_album_pb2.MyAlbumGetV1.Response,
        ),
    }
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "takasho.schema.fes.player_api.MyAlbum", album_handlers
            ),
        )
    )

    pub_servicer = PublishedPhotoService()
    pub_handlers = {
        "PublishV1": takasho_unary_handler(
            pub_servicer.PublishV1,
            published_photo_pb2.PublishedPhotoPublishV1.Request,
            published_photo_pb2.PublishedPhotoPublishV1.Response,
        ),
        "DeleteV1": takasho_unary_handler(
            pub_servicer.DeleteV1,
            published_photo_pb2.PublishedPhotoDeleteV1.Request,
            published_photo_pb2.PublishedPhotoDeleteV1.Response,
        ),
        "GetVarietyV1": takasho_unary_handler(
            pub_servicer.GetVarietyV1,
            published_photo_pb2.PublishedPhotoGetVarietyV1.Request,
            published_photo_pb2.PublishedPhotoGetVarietyV1.Response,
        ),
        "GetByPlayerIDV1": takasho_unary_handler(
            pub_servicer.GetByPlayerIDV1,
            published_photo_pb2.PublishedPhotoGetByPlayerIDV1.Request,
            published_photo_pb2.PublishedPhotoGetByPlayerIDV1.Response,
        ),
        "GetLikedV1": takasho_unary_handler(
            pub_servicer.GetLikedV1,
            published_photo_pb2.PublishedPhotoGetLikedV1.Request,
            published_photo_pb2.PublishedPhotoGetLikedV1.Response,
        ),
    }
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "takasho.schema.fes.player_api.PublishedPhoto", pub_handlers
            ),
        )
    )

    like_servicer = LikePublishedPhotoService()
    like_handlers = {
        "LikeV1": takasho_unary_handler(
            like_servicer.LikeV1,
            like_published_photo_pb2.LikePublishedPhotoLikeV1.Request,
            like_published_photo_pb2.LikePublishedPhotoLikeV1.Response,
        ),
        "DeleteLikeV1": takasho_unary_handler(
            like_servicer.DeleteLikeV1,
            like_published_photo_pb2.LikePublishedPhotoDeleteLikeV1.Request,
            like_published_photo_pb2.LikePublishedPhotoDeleteLikeV1.Response,
        ),
    }
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "takasho.schema.fes.player_api.LikePublishedPhoto", like_handlers
            ),
        )
    )
