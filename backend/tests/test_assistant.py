import pytest
from sqlalchemy import select

from app.ai.llm import LLMError, set_llm_override
from app.models.entities import AiRun
from tests.test_committee import FakeLLM, _plan


@pytest.fixture(autouse=True)
def _no_real_llm():
    set_llm_override(None)
    yield
    set_llm_override(None, active=False)


def _ask(client, h, q):
    r = client.post("/api/assistant/chat", json={"messages": [{"role": "user", "content": q}]}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def test_deterministic_top_opportunities(client, analyst, db):
    p, _ = _plan(db)
    db.commit()
    r = _ask(client, analyst, "Quali sono oggi le tre migliori opportunità?")
    assert r["mode"] == "deterministic"
    assert f"piano #{p.id}" in r["answer"] and "TEST.US" in r["answer"]
    assert r["sources"][0]["tool"] == "list_opportunities"
    assert r["sources"][0]["output"]["plans"][0]["plan_id"] == p.id


def test_deterministic_budget(client, analyst, db):
    _plan(db)
    db.commit()
    r = _ask(client, analyst, "Con 2.000 € disponibili, quale operazione è compatibile con i miei limiti di rischio?")
    src = r["sources"][0]
    assert src["tool"] == "check_budget" and src["input"] == {"available_eur": 2000.0}
    res = src["output"]["results"][0]
    assert res["notional_eur"] <= 2000 and res["quantity"] <= 10  # mai oltre la quantità del piano


def test_deterministic_invalidation_positions_performance(client, analyst, db):
    p, _ = _plan(db)
    db.commit()
    r = _ask(client, analyst, f"Cosa potrebbe invalidare il piano #{p.id}?")
    assert "Chiusura sotto lo stop 95" in r["answer"]
    assert "Non ci sono posizioni aperte" in _ask(client, analyst, "Quali posizioni richiedono attenzione?")["answer"]
    perf = _ask(client, analyst, "Come ha performato il sistema negli ultimi 90 giorni?")
    assert perf["sources"][0]["input"] == {"days": 90} and "non garantiscono" in perf["answer"]
    assert "riconosco" in _ask(client, analyst, "ciao")["answer"]


def test_ai_assistant_uses_tools_and_logs(client, analyst, db):
    p, _ = _plan(db)
    db.commit()
    set_llm_override(FakeLLM(tool_script=[("get_trade_plan", {"plan_id": p.id}), ("get_positions", {})]))
    r = _ask(client, analyst, "Perché hai selezionato questo titolo?")
    assert r["mode"] == "ai" and r["model"] == "fake-model"
    assert [s["tool"] for s in r["sources"]] == ["get_trade_plan", "get_positions"]
    assert r["sources"][0]["output"]["found"] is True
    run = db.execute(select(AiRun).where(AiRun.kind == "assistant")).scalar_one()
    assert run.mode == "ai" and run.prompt_hash and len(run.validation["tool_calls"]) == 2


def test_assistant_cannot_read_other_users_plans(client, analyst, admin, db):
    p, _ = _plan(db, email="admin@example.com")
    db.commit()
    set_llm_override(FakeLLM(tool_script=[("get_trade_plan", {"plan_id": p.id})]))
    r = _ask(client, analyst, "Dettagli?")
    assert r["sources"][0]["output"] == {"found": False}


def test_ai_failure_falls_back(client, analyst):
    class Broken(FakeLLM):
        def run_tools(self, **kw):
            raise LLMError("rete assente")
    set_llm_override(Broken())
    r = _ask(client, analyst, "Quali posizioni richiedono attenzione?")
    assert r["mode"] == "deterministic" and "rete assente" in r["answer"]


def test_chat_validation(client, analyst):
    r = client.post("/api/assistant/chat", json={"messages": [{"role": "assistant", "content": "x"}]}, headers=analyst)
    assert r.status_code == 422
