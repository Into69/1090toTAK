import logging
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from aircraft.registry import AircraftRegistry
from config import AppConfig

log = logging.getLogger(__name__)

_web_dir = Path(__file__).parent


def create_app(config: AppConfig, registry: AircraftRegistry, tak_sender=None, receiver=None, server_manager=None, store=None, gpsd_client=None):
    from .events import create_lifespan, setup_websocket

    lifespan = create_lifespan(config, registry, receiver)

    app = FastAPI(lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    templates = Jinja2Templates(directory=str(_web_dir / "templates"))

    app.mount("/static", StaticFiles(directory=str(_web_dir / "static")), name="static")

    from .routes import create_router
    router = create_router(config, registry, templates, tak_sender, receiver, server_manager, store, gpsd_client)
    app.include_router(router)

    setup_websocket(app, config, registry, receiver)

    return app
