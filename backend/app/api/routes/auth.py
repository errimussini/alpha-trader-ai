from fastapi import APIRouter, Depends
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import Unauthorized, current_user, require_role
from app.core.db import get_db
from app.core.errors import Conflict
from app.core.security import Role, create_access_token, hash_password, verify_password
from app.models.entities import User
from app.schemas.common import TokenOut, UserCreate, UserOut
from app.services import audit

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=TokenOut)
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.execute(select(User).where(User.email == form.username.lower())).scalar_one_or_none()
    if not user or not user.is_active or not verify_password(form.password, user.hashed_password):
        audit.record(db, actor=form.username.lower(), action="auth.login_failed")
        db.commit()
        raise Unauthorized("Credenziali non valide")
    audit.record(db, actor=user.email, action="auth.login", entity="user", entity_id=user.id)
    db.commit()
    return TokenOut(access_token=create_access_token(str(user.id), user.role), role=user.role)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)):
    return user


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(body: UserCreate, admin: User = Depends(require_role(Role.ADMIN)), db: Session = Depends(get_db)):
    email = body.email.lower()
    if db.execute(select(User).where(User.email == email)).scalar_one_or_none():
        raise Conflict("Utente già esistente")
    u = User(email=email, hashed_password=hash_password(body.password), role=body.role)
    db.add(u)
    db.flush()
    audit.record(db, actor=admin.email, action="auth.user_created", entity="user", entity_id=u.id,
                 payload={"email": email, "role": body.role})
    db.commit()
    return u
