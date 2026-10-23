"""Takasho OndemandMaster servicer for client boot and asset manifests."""

import grpc

from dankagu.config import settings
from dankagu.grpc.codec import takasho_unary_handler
from dankagu.grpc.generated.takasho.schema.common_featureset.player_api import (
    ondemand_master_pb2,
    ondemand_master_pb2_grpc,
)


class OndemandMasterService(ondemand_master_pb2_grpc.OndemandMasterServicer):
    """Servicer for OndemandMaster RPCs."""

    def __init__(self) -> None:
        manifest_meta = settings.data_dir / "metadata" / "AssetUploadV2+v2_1_0_any+g262.json"
        self._manifest_path = (
            manifest_meta
            if manifest_meta.exists()
            else settings.hexdata_cf_dir / "ondemand_master.AssetUploadV2+v2_1_0_any+g262.json"
        )
        self._cached_manifest: bytes = b""
        if self._manifest_path.exists():
            self._cached_manifest = self._manifest_path.read_bytes()

        self._gacha_entries: dict[str, list[bytes]] = {}

    async def _ensure_loaded(self) -> None:
        if self._gacha_entries:
            return

        import json

        from sqlalchemy import select

        from dankagu.core.database import get_sessionmaker
        from dankagu.models.master import MasterOndemandEntry

        try:
            session_factory = get_sessionmaker()
            async with session_factory() as session:
                result = await session.execute(select(MasterOndemandEntry))
                rows = result.scalars().all()
                for r in rows:
                    k = r.key
                    d = dict(r.value)
                    if k == "Gacha-163":
                        d["GachaBannerID"] = 165
                    elif k == "Gacha-167":
                        d["GachaBannerID"] = 169
                    elif k == "Gacha-171":
                        d["GachaBannerID"] = 173
                    elif k == "Gacha-3":
                        d["StartDashHours"] = 999999
                    val_bytes = json.dumps(d, ensure_ascii=False).encode("utf-8")
                    self._gacha_entries.setdefault(k, []).append(val_bytes)

                # Add alias keys if client requests them
                if "Gacha-163" in self._gacha_entries:
                    self._gacha_entries["Gacha-165"] = self._gacha_entries["Gacha-163"]
                if "Gacha-167" in self._gacha_entries:
                    self._gacha_entries["Gacha-169"] = self._gacha_entries["Gacha-167"]
                if "Gacha-171" in self._gacha_entries:
                    self._gacha_entries["Gacha-173"] = self._gacha_entries["Gacha-171"]
        except Exception:
            pass

    async def GetEntriesV1(
        self,
        request: ondemand_master_pb2.OndemandMasterGetEntriesV2.Request,
        context: grpc.aio.ServicerContext,
    ) -> ondemand_master_pb2.OndemandMasterGetEntriesV2.Response:
        await self._ensure_loaded()
        response = ondemand_master_pb2.OndemandMasterGetEntriesV2.Response()

        if "ODMGeneric_Boot" in request.keys:
            entry = response.entries.add()
            entry.key = "ODMGeneric_Boot"
            entry.value = (
                b'[{"CloseAt":"4102444800","Enable":2,"ID":1,"Key":"TosVer","OpenAt":"0","Value":"v1"},'
                b'{"CloseAt":"4102444800","Enable":2,"ID":2,"Key":"PPVer","OpenAt":"0","Value":"1"},'
                b'{"CloseAt":"4102444800","Enable":2,"ID":3,"Key":"TosBaseUrl","OpenAt":"0","Value":"{AssetBase}/static/tos"},'
                b'{"CloseAt":"4102444800","Enable":2,"ID":4,"Key":"PPBaseUrl","OpenAt":"0","Value":"{AssetBase}/static/pp"},'
                b'{"CloseAt":"4102444800","Enable":2,"ID":5,"Key":"AccountLinkApealTime","OpenAt":"0","Value":"2147483647"},'
                b'{"CloseAt":"4102444800","Enable":2,"ID":1001,"Key":"AdmobTester","OpenAt":"0","Value":""},'
                b'{"CloseAt":"4102444800","Enable":2,"ID":1002,"Key":"AdmobTestDevice","OpenAt":"0","Value":""},'
                b'{"CloseAt":"4102444800","Enable":2,"ID":1003,"Key":"AdmobAlwaysTestDevice","OpenAt":"0","Value":"0"},'
                b'{"CloseAt":"4102444800","Enable":2,"ID":1004,"Key":"AdmobTestClientVersion","OpenAt":"0","Value":"1.5.0"},'
                b'{"CloseAt":"4102444800","Enable":2,"ID":1011,"Key":"TestAdUnitId_A","OpenAt":"0","Value":""},'
                b'{"CloseAt":"4102444800","Enable":2,"ID":1012,"Key":"TestAdUnitId_I","OpenAt":"0","Value":""},'
                b'{"CloseAt":"4102444800","Enable":2,"ID":1013,"Key":"ProdAdUnitId_A","OpenAt":"0","Value":""},'
                b'{"CloseAt":"4102444800","Enable":2,"ID":1014,"Key":"ProdAdUnitId_I","OpenAt":"0","Value":""},'
                b'{"CloseAt":"4102444800","Enable":2,"ID":1015,"Key":"UseProdAdUnit","OpenAt":"0","Value":"0"},'
                b'{"CloseAt":"4102444800","Enable":2,"ID":1016,"Key":"UseProdAdUnitOnDebugBuild","OpenAt":"0","Value":"0"}]'
            )

        if "ODMGeneric_SecureHash" in request.keys:
            entry = response.entries.add()
            entry.key = "ODMGeneric_SecureHash"
            entry.value = (
                b'[{"CloseAt":"4102444800","Enable":2,"ID":1,"Key":"MasterEnable","OpenAt":"0","Value":"1"},'
                b'{"CloseAt":"4102444800","Enable":2,"ID":201001,"Key":"android_2.1.0_20220907","OpenAt":"0","Value":"d538802bf56581e8"},'
                b'{"CloseAt":"4102444800","Enable":2,"ID":201002,"Key":"ios_2.1.0_20220907","OpenAt":"0","Value":"d538802bf56581e8"}]'
            )

        if "ODMGeneric_Signature" in request.keys:
            entry = response.entries.add()
            entry.key = "ODMGeneric_Signature"
            entry.value = (
                b'[{"CloseAt":"4102444800","Enable":2,"ID":1,"Key":"MasterEnable","OpenAt":"0","Value":"1"},'
                b'{"CloseAt":"4102444800","Enable":2,"ID":2,"Key":"Sign_D","OpenAt":"0","Value":"db2d823ea4f7bcd40b6998a3b7a8adfd"},'
                b'{"CloseAt":"4102444800","Enable":2,"ID":4,"Key":"Sign_X","OpenAt":"0","Value":"14b869b2ba7332f4d633f21932b57147"},'
                b'{"CloseAt":"4102444800","Enable":2,"ID":5,"Key":"Sign_Xup","OpenAt":"0","Value":"cfb5a6c2695070d6642eb34b60e42048"}]'
            )

        if "AssetRootHash" in request.keys:
            entry = response.entries.add()
            entry.key = "AssetRootHash"
            entry.value = b'{"Roots":[{"Client":"n5rgXwR19ppZ_client","Key":"v2_1_0_any","OpenAt":123456789}]}'

        if "AssetUploadV2+v2_1_0_any+g262" in request.keys and self._cached_manifest:
            entry = response.entries.add()
            entry.key = "AssetUploadV2+v2_1_0_any+g262"
            entry.value = self._cached_manifest

        for key in request.keys:
            if key in self._gacha_entries:
                for val in self._gacha_entries[key]:
                    entry = response.entries.add()
                    entry.key = key
                    entry.value = val
            elif "Gacha-" in key:
                for k, vals in self._gacha_entries.items():
                    if k == key:
                        for val in vals:
                            entry = response.entries.add()
                            entry.key = key
                            entry.value = val

        return response


def register_ondemand_master_servicer(server: grpc.aio.Server) -> None:
    servicer = OndemandMasterService()
    method_handlers = {
        "GetEntriesV1": takasho_unary_handler(
            servicer.GetEntriesV1,
            ondemand_master_pb2.OndemandMasterGetEntriesV2.Request,
            ondemand_master_pb2.OndemandMasterGetEntriesV2.Response,
        )
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "takasho.schema.common_featureset.player_api.OndemandMaster", method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))
