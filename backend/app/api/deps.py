from collections.abc import Callable

import jwt
from fastapi import Depends, Query
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.adapters.market_data.base import MarketDataAdapter
from app.adapters.market_data.registry import get_market_data_adapter
from app.core.db import get_db
from app.core.errors import AppError, Forbidden
from app.core.security import ROLE_RANK, Role, decode_token
from app.models.entities import User

oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"


def current_user(token: str | None = Depends(oauth2), db: Session = Depends(get_db)) -> User:
    if not token:
        raise Unauthorized("Autenticazione richiesta")
    try:
        payload = decode_token(token)
    except jwt.ExpiredSignatureError as e:
        raise Unauthorized("Sessione scaduta") from e
    except jwt.PyJWTError as e:
        raise Unauthorized("Token non valido") from e
    user = db.get(User, int(payload["sub"]))
    if not user or not user.is_active:
        raise Unauthorized("Utente non attivo")
    return user


def require_role(min_role: Role) -> Callable[..., User]:
    def dep(user: User = Depends(current_user)) -> User:
        if ROLE_RANK[Role(user.role)] < ROLE_RANK[min_role]:
            raise Forbidden(f"Ruolo '{min_role}' richiesto")
        return user
    return dep


def market_data() -> MarketDataAdapter:
    return get_market_data_adapter()


class Pagination:
    def __init__(self, page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=200)):
        self.page = page
        self.page_size = page_size

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size
