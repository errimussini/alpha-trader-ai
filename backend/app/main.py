from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from app.api.routes import auth, market, portfolio, scanner, system, trading
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import install_error_handlers
from app.core.security import hash_password
from app.models.entities import User
from app.services import audit


def bootstrap_admin() -> None:
    """Crea l'amministratore iniziale se configurato via env e se non esiste alcun utente."""
    s = get_settings()
    if not (s.bootstrap_admin_email and s.bootstrap_admin_password):
        return
    with SessionLocal() as db:
        if db.execute(select(User.id).limit(1)).first():
            return
        u = User(email=s.bootstrap_admin_email.lower(),
                 hashed_password=hash_password(s.bootstrap_admin_password.get_secret_value()), role="admin")
        db.add(u)
        db.flush()
        audit.record(db, actor="system", action="auth.bootstrap_admin", entity="user", entity_id=u.id)
        db.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if get_settings().environment != "test":
        bootstrap_admin()
    yield


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title=s.app_name, version="0.1.0", lifespan=lifespan,
                  description="Research & Paper Trading. Nessun rendimento è garantito. Live trading disattivato.")
    app.add_middleware(CORSMiddleware, allow_origins=s.cors_origins, allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])
    install_error_handlers(app)
    for r in (auth.router, system.router, market.router, scanner.router, trading.router, portfolio.router):
        app.include_router(r)
    return app


app = create_app()
