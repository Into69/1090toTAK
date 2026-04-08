import secrets
import logging
from flask import Flask
from flask_socketio import SocketIO

from aircraft.registry import AircraftRegistry
from config import AppConfig

log = logging.getLogger(__name__)


def create_app(config: AppConfig, registry: AircraftRegistry, tak_sender=None, receiver=None, server_manager=None, store=None, gpsd_client=None):
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    app.config["SECRET_KEY"] = secrets.token_hex(16)

    socketio = SocketIO(
        app,
        async_mode="threading",
        cors_allowed_origins="*",
        transports=["polling"],
        logger=False,
        engineio_logger=False,
    )
    # Suppress the spurious "WebSocket transport not available" error that
    # Engine.IO emits on every connection.  WebSocket is intentionally disabled
    # (transports=["polling"]) because Waitress is a pure-WSGI server and does
    # not support the HTTP upgrade required for WebSocket.
    logging.getLogger("engineio.server").setLevel(logging.CRITICAL)

    from .routes import register_routes
    from .events import register_events

    register_routes(app, config, registry, tak_sender, receiver, server_manager, store, gpsd_client)
    register_events(socketio, config, registry, receiver)

    return app, socketio
