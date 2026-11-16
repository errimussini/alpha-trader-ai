from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.ai.committee import AGENTS, allowed_decisions, build_dossiers, run_committee, unverified_numbers
from app.ai.llm import LLMError, LLMResult, set_llm_override
from app.models.entities import AiRun, Instrument, Job, Position, RiskAssessment, TradePlan, User
from app.services import jobs
from app.services.portfolio import get_or_create_paper_portfolio


class FakeLLM:
    """Modello finto: restituisce output validi secondo lo schema richiesto e registra le chiamate."""
    model = "fake-model"

    def __init__(self, decision=None, fail_agents=(), invent_number=False, tool_script=None):
        self.decision, self.fail_agents, self.invent = decision, set(fail_agents), invent_number
        self.calls, self.tool_script = [], tool_script or []

    def structured(self, *, system, user, schema, max_tokens=None):
        self.calls.append({"system": system, "schema": schema})
        if "decision" in schema["properties"]:
            allowed = schema["properties"]["decision"]["enum"]
            return LLMResult({"decision": self.decision or allowed[0], "summary": "Sintesi del comitato.",
                              "reasons_for": ["Trend confermato"], "reasons_against": ["Regime incerto"],
                              "could_be_wrong_if": ["Chiusura sotto lo stop"], "conditions_to_act": ["Conferma volumi"],
                              "dissent": [], "data_refs": ["piano.stato", "chiave.inesistente"]},
                             {"model": "fake-model", "usage": {"input_tokens": 10, "output_tokens": 5}})
        if any(t in system for t in self.fail_agents):
            raise LLMError("errore simulato")
        txt = "Stop a 95 e ingresso a 100." + (" Probabilità di successo 73,5%." if self.invent else "")
        return LLMResult({"stance": "FAVOREVOLE", "summary": txt, "points_for": ["a"], "points_against": [], "risks": [],
                          "data_refs": ["piano.ingresso", "non.esiste"]}, {"model": "fake-model", "usage": {}})

    def run_tools(self, *, system, messages, tools, handler, max_steps):
        calls = []
        for name, args in self.tool_script:
            calls.append({"tool": name, "input": args, "output": handler(name, args)})
        return LLMResult("Risposta basata sugli strumenti.", {"model": "fake-model", "tool_calls": calls, "usage": {}})


@pytest.fixture(autouse=True)
def _no_real_llm():
    set_llm_override(None)
    yield
    set_llm_override(None, active=False)


def _plan(db, email="analyst@example.com", status="READY", approved=True, qty="10"):
    user = db.execute(select(User).where(User.email == email)).scalar_one()
    pf = get_or_create_paper_portfolio(db, user)
    inst = Instrument(symbol="TEST", exchange="US", name="Test Corp", currency="EUR", data_source="test", sector="Tech")
    db.add(inst)
    db.flush()
    ra = RiskAssessment(portfolio_id=pf.id, approved=approved, inputs={"equity": "10000", "cash": "10000", "open_risk": "0"},
                        result={"binding_constraint": "risk_budget", "exposure_pct": "10.00", "aggregate_risk_after_pct": "0.50",
                                "est_loss_gap_scenario_base": "60.00", "warnings": []},
                        blocking_reasons=[] if approved else ["Quantità nulla"], engine_version="t")
    db.add(ra)
    db.flush()
    now = datetime.now(UTC)
    p = TradePlan(instrument_id=inst.id, portfolio_id=pf.id, risk_assessment_id=ra.id, model_version="t", strategy_style="SWING",
                  setup="PULLBACK", score=D("70"), risk_level="MEDIUM", market_regime="BULL", price_reference=D("100"),
                  price_source="test", price_time=now, entry_price=D("100"), entry_low=D("99"), entry_high=D("101"),
                  stop_loss=D("95"), take_profit_1=D("110"), take_profit_2=D("115"), quantity=D(qty), notional_base=D("1000"),
                  est_loss_at_stop_base=D("50"), reward_risk_net=D("1.9"), horizon_days=30, status=status,
                  valid_until=now + timedelta(days=3), currency="EUR", invalidation_conditions=["Chiusura sotto lo stop 95"],
                  entry_conditions=["Tenuta della EMA20"])
    db.add(p)
    db.flush()
    return p, pf


def test_allowed_decisions_are_deterministic(client, analyst, db):
    p, pf = _plan(db)
    assert allowed_decisions(p, build_dossiers(db, p))[0] == ["BUY", "WAIT", "REJECT"]
    p.status = "WAIT"
    assert allowed_decisions(p, build_dossiers(db, p))[0] == ["WAIT", "REJECT"]
    p.status = "REJECTED"
    assert allowed_decisions(p, build_dossiers(db, p))[0] == ["REJECT"]
    p.status = "READY"
    db.add(Position(portfolio_id=pf.id, instrument_id=p.instrument_id, quantity=D("5"), avg_price=D("98"), currency="EUR"))
    db.flush()
    assert allowed_decisions(p, build_dossiers(db, p))[0] == ["HOLD", "REDUCE", "EXIT"]


def test_risk_veto_forces_reject_even_with_ai(client, analyst, db):
    p, _ = _plan(db, status="WAIT", approved=False, qty="0")
    llm = FakeLLM(decision="REJECT")
    res = run_committee(db, p, llm, "test")
    # lo schema inviato al modello ammette solo REJECT
    assert llm.calls[-1]["schema"]["properties"]["decision"]["enum"] == ["REJECT"]
    assert res["decision"]["decision"] == "REJECT" and res["allowed_decisions"] == ["REJECT"]


def test_deterministic_committee_without_key(client, analyst, db):
    p, _ = _plan(db)
    res = run_committee(db, p, None, "test")
    assert res["mode"] == "deterministic" and len(res["agents"]) == len(AGENTS) == 6
    assert res["decision"]["decision"] in res["allowed_decisions"]
    assert res["decision"]["could_be_wrong_if"]
    db.commit()
    assert db.execute(select(AiRun).where(AiRun.trade_plan_id == p.id)).scalars().all().__len__() == 7


def test_ai_committee_validates_and_flags(client, analyst, db):
    p, _ = _plan(db)
    llm = FakeLLM(decision="WAIT", invent_number=True, fail_agents=["Agent 4"])
    res = run_committee(db, p, llm, "test")
    assert res["mode"] == "mixed"  # un agente è ricaduto sulla modalità deterministica
    by = {a["agent"]: a for a in res["agents"]}
    assert by["fundamental_analyst"]["mode"] == "deterministic" and by["fundamental_analyst"]["error"]
    tech = by["technical_analyst"]
    assert tech["mode"] == "ai"
    assert "non.esiste" in tech["validation"]["unknown_refs"] and "non.esiste" not in tech["data_refs"]
    assert "73,5" in tech["validation"]["unverified_numbers"]  # numero non presente nei dati: segnalato
    assert "95" not in tech["validation"]["unverified_numbers"] and "100" not in tech["validation"]["unverified_numbers"]
    assert res["decision"]["decision"] == "WAIT" and res["decision"]["mode"] == "ai"
    runs = db.execute(select(AiRun).where(AiRun.trade_plan_id == p.id)).scalars().all()
    assert all(r.prompt_version and r.input_hash for r in runs)
    assert any(r.status == "FALLBACK" for r in runs)


def test_unverified_numbers():
    src = {"a": 101.25, "b": "0.0525", "c": [{"d": 3000}]}
    assert unverified_numbers(["Prezzo 101,25 e rendimento 5,25% su 3.000 €"], src) == []
    assert unverified_numbers(["Obiettivo 140"], src) == ["140"]
    assert unverified_numbers(["TP1, EMA20, 3 giorni"], src) == []


def test_committee_api_and_job_queue(client, analyst, viewer, db):
    p, _ = _plan(db)
    db.commit()
    assert client.post(f"/api/opportunities/{p.id}/committee", headers=viewer).status_code == 403
    r = client.post(f"/api/opportunities/{p.id}/committee", headers=analyst)
    assert r.status_code == 200 and r.json()["committee"]["status"] == "DONE"  # senza chiave: immediato
    # con modello: accodato e poi eseguito dal worker
    set_llm_override(FakeLLM(decision="BUY"))
    r = client.post(f"/api/opportunities/{p.id}/committee", headers=analyst).json()["committee"]
    assert r["status"] == "QUEUED" and r["previous"]["status"] == "DONE"
    again = client.post(f"/api/opportunities/{p.id}/committee", headers=analyst).json()["committee"]
    assert again["job_id"] == r["job_id"]  # nessun duplicato
    db.expire_all()
    job = jobs.run_one(db)
    assert job.status == "DONE", job.error
    c = client.get(f"/api/opportunities/{p.id}/committee", headers=analyst).json()["committee"]
    assert c["status"] == "DONE" and c["decision"]["decision"] == "BUY" and c["model"] == "fake-model"
    assert db.get(Job, job.id).result["decision"] == "BUY"
    runs = client.get(f"/api/ai/runs?trade_plan_id={p.id}", headers=analyst).json()
    assert runs["total"] == 14


def test_ai_status(client, analyst):
    assert client.get("/api/ai/status", headers=analyst).json()["mode"] == "deterministic"
    set_llm_override(FakeLLM())
    assert client.get("/api/ai/status", headers=analyst).json() == {
        "enabled": True, "model": "fake-model", "configured_model": "claude-opus-5-5", "effort": "medium", "mode": "ai"}
