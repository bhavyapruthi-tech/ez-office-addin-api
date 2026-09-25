from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.auth import router as auth_router
from app.api.wallet import router as wallet_router
from app.api.webhooks import router as webhooks_router
from app.core.config import settings
from app.core.errors import register_exception_handlers


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="EZ Office Add-in Backend", lifespan=lifespan)

    # R18/KD13: explicit non-wildcard allow-list, never allow_origins=["*"],
    # since every route below returns bearer-authenticated data.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.addin_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(wallet_router)
    app.include_router(webhooks_router)

    return app


app = create_app()
