"""Takasho & Envoy gRPC metadata interceptor with detailed logging."""

import logging
import time
from collections.abc import Callable
from email.utils import formatdate
from typing import Any

import grpc
from grpc.aio import ServerInterceptor

from dankagu.grpc.codec import default_packer

logger = logging.getLogger("dankagu.grpc")


class TakashoAsyncInterceptor(ServerInterceptor):
    """Async gRPC interceptor injecting Takasho and Envoy headers into metadata."""

    async def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Any],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> Any:
        method_name = handler_call_details.method

        handler = await continuation(handler_call_details)
        if handler is None:
            logger.warning("⚠️  [gRPC Auto-Mock Fallback] Unregistered RPC: %s", method_name)

            async def fallback_behavior(raw_request: bytes, context: grpc.aio.ServicerContext) -> bytes:
                start_ns = time.perf_counter_ns()
                server_timestamp = str(int(time.time()))
                date_header = formatdate(timeval=None, localtime=False, usegmt=True)

                try:
                    unpacked = default_packer.unpack(raw_request)
                    logger.info("🎮 [gRPC REQ Auto-Mock] ---> %s (raw %d B, unpacked %d B)", method_name, len(raw_request), len(unpacked))
                except Exception:
                    logger.info("🎮 [gRPC REQ Auto-Mock] ---> %s (raw %d B)", method_name, len(raw_request))

                await context.send_initial_metadata((
                    ("x-takasho-debug-adjustment-timestamp", server_timestamp),
                    ("x-takasho-requested-timestamp", server_timestamp),
                    ("date", date_header),
                    ("server", "envoy"),
                ))

                elapsed_ms = (time.perf_counter_ns() - start_ns) // 1_000_000
                context.set_trailing_metadata((
                    ("x-envoy-upstream-service-time", str(elapsed_ms)),
                    ("x-takasho-respond-timestamp", str(int(time.time()))),
                    ("x-takasho-server-version", "v1.16.3"),
                ))

                response = default_packer.pack(b"")
                logger.info("🎮 [gRPC RESP Auto-Mock] <--- %s (%d ms, empty packed protobuf)", method_name, elapsed_ms)
                return response

            return grpc.unary_unary_rpc_method_handler(
                fallback_behavior,
                request_deserializer=lambda x: x,
                response_serializer=lambda x: x,
            )

        if handler.unary_unary:
            original_behavior = handler.unary_unary

            async def wrapped_behavior(request: Any, context: grpc.aio.ServicerContext) -> Any:
                start_ns = time.perf_counter_ns()
                server_timestamp = str(int(time.time()))
                date_header = formatdate(timeval=None, localtime=False, usegmt=True)

                req_str = str(request).strip()
                if not req_str:
                    req_str = "(empty message)"
                elif len(req_str) > 1000:
                    req_str = req_str[:1000] + "... [truncated]"

                logger.info("🎮 [gRPC REQ] ---> %s\n   Payload: %s", method_name, req_str)

                await context.send_initial_metadata((
                    ("x-takasho-debug-adjustment-timestamp", server_timestamp),
                    ("x-takasho-requested-timestamp", server_timestamp),
                    ("date", date_header),
                    ("server", "envoy"),
                ))

                try:
                    response = await original_behavior(request, context)
                except Exception as exc:
                    logger.error("💥 [gRPC Error] %s failed with exception: %s", method_name, exc, exc_info=True)
                    raise

                elapsed_ms = (time.perf_counter_ns() - start_ns) // 1_000_000
                context.set_trailing_metadata((
                    ("x-envoy-upstream-service-time", str(elapsed_ms)),
                    ("x-takasho-respond-timestamp", str(int(time.time()))),
                    ("x-takasho-server-version", "v1.16.3"),
                ))

                resp_str = str(response).strip()
                if not resp_str:
                    resp_str = "(empty message)"
                elif len(resp_str) > 1000:
                    resp_str = resp_str[:1000] + "... [truncated]"

                logger.info("🎮 [gRPC RESP] <--- %s (%d ms)\n   Payload: %s", method_name, elapsed_ms, resp_str)
                return response

            return grpc.unary_unary_rpc_method_handler(
                wrapped_behavior,
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )

        return handler
