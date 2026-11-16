"""Comandi di amministrazione.

    python -m app.cli create-user EMAIL --role admin
"""
import argparse
import getpass
import sys

from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.security import hash_password
from app.models.entities import User
from app.services import audit


def create_user(email: str, role: str) -> None:
    pw = getpass.getpass("Password (min 10 caratteri): ")
    if len(pw) < 10 or pw != getpass.getpass("Conferma: "):
        sys.exit("Password non valida o non coincidente")
    with SessionLocal() as db:
        if db.execute(select(User).where(User.email == email.lower())).scalar_one_or_none():
            sys.exit("Utente già esistente")
        u = User(email=email.lower(), hashed_password=hash_password(pw), role=role)
        db.add(u)
        db.flush()
        audit.record(db, actor="cli", action="auth.user_created", entity="user", entity_id=u.id,
                     payload={"email": u.email, "role": role})
        db.commit()
    print(f"Creato {email} ({role})")


def main() -> None:
    ap = argparse.ArgumentParser(prog="app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    cu = sub.add_parser("create-user")
    cu.add_argument("email")
    cu.add_argument("--role", choices=["admin", "analyst", "viewer"], default="admin")
    a = ap.parse_args()
    if a.cmd == "create-user":
        create_user(a.email, a.role)


if __name__ == "__main__":
    main()
