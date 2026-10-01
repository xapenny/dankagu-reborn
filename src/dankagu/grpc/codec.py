"""Takasho gRPC serialization codec helpers."""

from collections.abc import Callable
from typing import Any

import grpc
from google.protobuf.message import Message

from dankagu.core.packer import default_packer


def takasho_unary_handler[M: Message](
    behavior: Callable[[Any, grpc.aio.ServicerContext], Any],
    request_type: type[M],
    response_type: type[M],
) -> grpc.RpcMethodHandler:
    """Create a gRPC unary-unary method handler with Takasho Packer framing."""
    return grpc.unary_unary_rpc_method_handler(
        behavior,
        request_deserializer=lambda raw: request_type.FromString(default_packer.unpack(raw)),
        response_serializer=lambda msg: default_packer.pack(msg.SerializeToString()),
    )
