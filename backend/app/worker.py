"""Worker dei job in background.

    python -m app.worker

- Coda nel database: prende una scansione QUEUED con SELECT ... FOR UPDATE SKIP LOCKED (più worker sicuri).
- Scheduling: nei giorni feriali, all'orario SCAN_SCHEDULE_UTC, accoda una scansione con chiave di
  idempotenza "scheduled:AAAA-MM-GG" (mai due scansioni programmate nello stesso giorno).
- Recupero: una scansione RUNNING senza heartbeat da oltre 30 minuti viene marcata FAILED.
- Hook per i job delle fasi successive (monitoraggio posizioni) tramite la lista PERIODIC_JOBS.
"""
from __future__ import annotations

import logging
import signal
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.adapters.market_data.registry import get_market_data_adapter
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.entities import Alert, ScanRun
from app.services import audit
from app.services.planner import expire_plans, generate_for_scan, plans_for_user_portfolios
from app.services.scanner import enqueue_scan, run_scan

log = logging.getLogger("worker")
STALE_AFTER = timedelta(minutes=30)
def _expire_job() -> None:
    with SessionLocal() as db:
        n = expire_plans(db)
        db.commit()
        if n:
            log.info("%s piani scaduti", n)


PERIODIC_JOBS: list[tuple[str, int, Callable[[], None]]] = [  # (nome, intervallo s, funzione)
    ("expire_plans", 600, _expire_job),
]
_stop = False


def _handle(*_):
    global _stop
    _stop = True


def recover_stale() -> None:
    with SessionLocal() as db:
        limit = datetime.now(UTC) - STALE_AFTER
        for sr in db.execute(select(ScanRun).where(ScanRun.status == "RUNNING")).scalars():
            hb = sr.heartbeat_at or sr.started_at
            if hb is None or hb < limit:
                sr.status, sr.finished_at = "FAILED", datetime.now(UTC)
                sr.errors = (sr.errors or []) + [{"stage": "worker", "error": "heartbeat scaduto"}]
                db.add(Alert(kind="SYSTEM", severity="WARNING", message=f"Scansione #{sr.id} interrotta (heartbeat scaduto)"))
        db.commit()


def maybe_schedule(now: datetime) -> None:
    hhmm = get_settings().scan_schedule_utc
    if not hhmm or now.weekday() >= 5:
        return
    h, m = (int(x) for x in hhmm.split(":"))
    if (now.hour, now.minute) < (h, m):
        return
    with SessionLocal() as db:
        sr, created = enqueue_scan(db, actor="scheduler", trigger="SCHEDULED",
                                   idempotency_key=f"scheduled:{now.date().isoformat()}")
        db.commit()
        if created:
            log.info("scansione programmata accodata #%s", sr.id)


def claim_and_run() -> bool:
    with SessionLocal() as db:
        sr = db.execute(select(ScanRun).where(ScanRun.status == "QUEUED").order_by(ScanRun.id)
                        .with_for_update(skip_locked=True).limit(1)).scalar_one_or_none()
        if sr is None:
            return False
        sr.status, sr.started_at, sr.heartbeat_at = "RUNNING", datetime.now(UTC), datetime.now(UTC)
        db.commit()
        try:
            adapter = get_market_data_adapter()
            run_scan(db, adapter, sr)
            log.info("scansione #%s completata: %s", sr.id, sr.stats)
            for pf, owner in plans_for_user_portfolios(db):
                plans = generate_for_scan(db, adapter, sr, pf, "worker", get_settings().plan_top_n)
                db.commit()
                log.info("portafoglio #%s: %s piani generati", pf.id, len(plans))
        except Exception as e:  # noqa: BLE001
            db.rollback()
            sr = db.get(ScanRun, sr.id)
            sr.status, sr.finished_at = "FAILED", datetime.now(UTC)
            sr.errors = (sr.errors or []) + [{"stage": "scan", "error": f"{type(e).__name__}: {str(e)[:500]}"}]
            db.add(Alert(kind="SYSTEM", severity="WARNING", message=f"Scansione #{sr.id} fallita: {str(e)[:200]}"))
            audit.record(db, actor="worker", action="scan.failed", entity="scan_run", entity_id=sr.id,
                         payload={"error": str(e)[:500]})
            db.commit()
            log.exception("scansione #%s fallita", sr.id)
        return True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    last_run: dict[str, float] = {}
    log.info("worker avviato")
    while not _stop:
        try:
            recover_stale()
            maybe_schedule(datetime.now(UTC))
            for name, every, fn in PERIODIC_JOBS:
                if time.monotonic() - last_run.get(name, 0) >= every:
                    last_run[name] = time.monotonic()
                    try:
                        fn()
                    except Exception:  # noqa: BLE001
                        log.exception("job %s fallito", name)
            if claim_and_run():
                continue
        except Exception:  # noqa: BLE001
            log.exception("errore nel ciclo del worker")
        time.sleep(5)


if __name__ == "__main__":
    main()
