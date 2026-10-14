"""Explicit failure reporting for gRPC handlers.

A handler that swallows an exception and answers with a normal (or empty) response
is indistinguishable, on the client, from "the player really has no data" — which
is how player state silently disappears. Handlers must either succeed or fail
visibly.

Usage::

    @fail_internal_on_error("Failed to persist player storage entries")
    async def SetEntriesV2(self, request, context): ...

Deliberate ``context.abort(...)`` calls pass through untouched, so a handler keeps
control of its own status codes (UNAUTHENTICATED, NOT_FOUND, ...).
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import grpc

logger = logging.getLogger("dankagu.grpc.errors")

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


async def fail_internal(
    context: grpc.aio.ServicerContext,
    message: str,
    exc: BaseException,
    **details: object,
) -> None:
    """Log ``exc`` with context and abort the RPC with ``INTERNAL``.

    Never returns: ``context.abort`` raises.
    """
    detail_text = " ".join(f"{key}={value!r}" for key, value in details.items())
    logger.error(
        "❌ %s%s: %s",
        message,
        f" ({detail_text})" if detail_text else "",
        exc,
        exc_info=exc,
    )
    await context.abort(
        grpc.StatusCode.INTERNAL,
        f"{message}. The server refused to answer with possibly wrong data.",
    )


def fail_internal_on_error(message: str) -> Callable[[F], F]:
    """Turn any unexpected exception in a unary handler into a logged INTERNAL error."""

    def decorator(function: F) -> F:
        @functools.wraps(function)
        async def wrapper(
            self: Any, request: Any, context: grpc.aio.ServicerContext, *args: Any, **kwargs: Any
        ) -> Any:
            try:
                return await function(self, request, context, *args, **kwargs)
            except grpc.aio.AioRpcError:
                # A deliberate abort() from the handler itself (or from a nested
                # call); it already carries the status the handler chose.
                raise
            except grpc.RpcError:
                raise
            except Exception as exc:
                player_id = None
                try:
                    from dankagu.core.session import get_current_player_id

                    player_id = get_current_player_id()
                except Exception:  # pragma: no cover - context is best effort
                    pass
                await fail_internal(
                    context,
                    message,
                    exc,
                    handler=getattr(function, "__name__", "?"),
                    player_id=player_id,
                )
                raise  # unreachable: abort() raised; keeps type checkers happy

        return wrapper  # type: ignore[return-value]

    return decorator
