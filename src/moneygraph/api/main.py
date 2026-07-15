import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .routers import candidates, graph, nodes, pipeline, settings

# Uvicorn claims the root logger at WARNING level, which swallows INFO from
# our background tasks. Add an explicit handler on the "app" namespace so
# edgar.py and pipeline.py logs are always visible.
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(levelname)-8s %(name)s  %(message)s"))
_app_log = logging.getLogger("app")
_app_log.setLevel(logging.INFO)
_app_log.addHandler(_handler)
_app_log.propagate = False  # don't double-emit via uvicorn's root handler

logger = logging.getLogger("app")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    from .scheduler import shutdown_scheduler, start_scheduler

    start_scheduler()
    yield
    shutdown_scheduler()


def create_app() -> FastAPI:
    app = FastAPI(title="AI Investment Graph API", lifespan=_lifespan)
    app.include_router(nodes.router)
    app.include_router(graph.router)
    app.include_router(pipeline.router)
    app.include_router(candidates.router)
    app.include_router(settings.router)
    return app


app = create_app()
