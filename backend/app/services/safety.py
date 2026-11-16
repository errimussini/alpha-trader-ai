"""Controlli di sicurezza trasversali: modalità operativa, kill switch, freschezza dei dati."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError, SafetyBlock
from app.models.entities import SystemSetting, TradingMode
from app.services import audit

MODE_KEY = "trading_mode"
KILL_KEY = "kill_switch"
ALLOWED_MODES = {TradingMode.RESEARCH, TradingMode.PAPER}


def _get(db: Session, key: str) -> dict | None:
    row = db.get(SystemSetting, key)
    return row.value if row else None


def _set(db: Session, key: str, value: dict, actor: str) -> None:
    row = db.get(SystemSetting, key)
    if row:
        row.value = value
        row.updated_by = actor
    else:
        db.add(SystemSetting(key=key, value=value, updated_by=actor))


def get_mode(db: Session) -> str:
    v = _get(db, MODE_KEY)
    return v["mode"] if v else get_settings().default_mode


def set_mode(db: Session, mode: str, actor: str) -> str:
    if mode == TradingMode.LIVE_ASSISTED or mode not in ALLOWED_MODES:
        raise AppError("La modalità Live non è disponibile in questa release: solo RESEARCH e PAPER.",
                       code="mode_not_allowed")
    old = get_mode(db)
    _set(db, MODE_KEY, {"mode": mode}, actor)
    audit.record(db, actor=actor, action="system.mode_changed", entity="system", payload={"from": old, "to": mode})
    return mode


def kill_switch_state(db: Session) -> dict:
    return _get(db, KILL_KEY) or {"active": False, "reason": None, "since": None}


def set_kill_switch(db: Session, active: bool, reason: str | None, actor: str) -> dict:
    state = {"active": active, "reason": reason, "since": datetime.now(UTC).isoformat() if active else None}
    _set(db, KILL_KEY, state, actor)
    audit.record(db, actor=actor, action="system.kill_switch", entity="system", payload=state)
    return state


def assert_can_trade_paper(db: Session) -> None:
    ks = kill_switch_state(db)
    if ks["active"]:
        raise SafetyBlock(f"Kill switch attivo: {ks.get('reason') or 'nessun motivo indicato'}", code="kill_switch")
    if get_mode(db) != TradingMode.PAPER:
        raise SafetyBlock("Ordini simulati consentiti solo in Paper Trading Mode (modalità attuale: Research).",
                          code="wrong_mode")


def quote_age(quote_time: datetime, now: datetime | None = None) -> timedelta:
    now = now or datetime.now(UTC)
    if quote_time.tzinfo is None:
        quote_time = quote_time.replace(tzinfo=UTC)
    return now - quote_time


def assert_fresh(quote_time: datetime, *, what: str, max_minutes: int | None = None) -> None:
    max_minutes = max_minutes if max_minutes is not None else get_settings().max_quote_age_minutes
    age = quote_age(quote_time)
    if age > timedelta(minutes=max_minutes):
        raise SafetyBlock(
            f"Dati obsoleti per {what}: quotazione di {int(age.total_seconds() // 60)} minuti fa "
            f"(limite {max_minutes}). Operazione sospesa.", code="stale_data",
            details={"quote_time": quote_time.isoformat(), "max_age_minutes": max_minutes},
        )
