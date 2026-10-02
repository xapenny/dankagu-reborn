"""AsyncIO gRPC server for Takasho API."""

import logging

import grpc

from dankagu.config import settings
from dankagu.grpc.interceptors import TakashoAsyncInterceptor
from dankagu.services.announcement import register_announcement_servicer
from dankagu.services.baas_product import register_baas_product_servicer
from dankagu.services.club import register_club_servicers
from dankagu.services.friend import register_friend_servicer
from dankagu.services.game_product import register_game_product_servicer
from dankagu.services.game_status import register_game_status_servicer
from dankagu.services.login_bonus import register_login_bonus_servicer
from dankagu.services.loot_box import register_loot_box_servicer
from dankagu.services.ondemand_master import register_ondemand_master_servicer
from dankagu.services.player_event_log import register_player_event_log_servicer
from dankagu.services.player_inventory import register_player_inventory_servicer
from dankagu.services.player_key_value_store import register_player_key_value_store_servicer
from dankagu.services.player_preference import register_player_preference_servicer
from dankagu.services.player_storage import register_player_storage_servicer
from dankagu.services.push_notification import register_push_notification_servicer
from dankagu.services.ranking import register_ranking_servicers
from dankagu.services.reconstruction_spot import register_reconstruction_spot_servicer
from dankagu.services.step_up_loot_box import register_step_up_loot_box_servicer
from dankagu.services.subscription import register_subscription_servicers
from dankagu.services.system import register_system_servicer
from dankagu.services.wallet import register_wallet_servicer
from dankagu.services.zendesk import register_zendesk_servicer

logger = logging.getLogger("dankagu.grpc.server")


def create_grpc_server() -> grpc.aio.Server:
    """Create and configure the async gRPC server with Takasho services."""
    server = grpc.aio.server(interceptors=[TakashoAsyncInterceptor()])

    register_ondemand_master_servicer(server)
    register_system_servicer(server)
    register_player_storage_servicer(server)
    register_player_preference_servicer(server)
    register_wallet_servicer(server)
    register_push_notification_servicer(server)
    register_game_status_servicer(server)
    register_game_product_servicer(server)
    register_loot_box_servicer(server)
    register_step_up_loot_box_servicer(server)
    register_announcement_servicer(server)
    register_player_inventory_servicer(server)
    register_baas_product_servicer(server)
    register_player_key_value_store_servicer(server)
    register_player_event_log_servicer(server)

    # Game Domain (Fes) Services
    register_login_bonus_servicer(server)
    register_reconstruction_spot_servicer(server)
    register_friend_servicer(server)
    register_club_servicers(server)
    register_ranking_servicers(server)
    register_subscription_servicers(server)
    register_zendesk_servicer(server)

    # Plaintext gRPC port for clients that connect without TLS.
    server.add_insecure_port(f"{settings.host}:{settings.grpc_port}")

    # TLS port for clients that require an encrypted connection. Certificates
    # are generated locally (they are not distributed with the repository), so
    # treat them as optional and never hand empty credential material to gRPC.
    cert_path = settings.certs_dir / "server.crt"
    key_path = settings.certs_dir / "server.key"
    if (
        cert_path.exists()
        and key_path.exists()
        and cert_path.stat().st_size
        and key_path.stat().st_size
    ):
        server_credentials = grpc.ssl_server_credentials(
            [(key_path.read_bytes(), cert_path.read_bytes())]
        )
        server.add_secure_port(f"{settings.host}:{settings.grpc_tls_port}", server_credentials)
    else:
        logger.warning(
            "TLS certificates not found in %s - gRPC TLS port %d disabled. "
            "Run 'python -m dankagu.core.certs' to generate them.",
            settings.certs_dir,
            settings.grpc_tls_port,
        )

    return server
