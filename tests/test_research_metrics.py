"""M7 · métricas para el análisis (core/research_metrics.py): solo lectura, desde el registro de eventos (M3)."""

import pytest

from core import dispatch, event_log, ludica, options, research_metrics, service_requests

ITEM = "LUD-001"
STUDENT = {"id": "s1", "full_name": "Cliente 01", "institutional_email": "c01@ludica.invalid", "role": "estudiante"}
OPERATOR = {"id": "o1", "full_name": "Operario 01", "institutional_email": "o01@ludica.invalid", "role": "operario"}
SUPERVISOR = {"id": "p1", "full_name": "Supervisor 01", "institutional_email": "p01@ludica.invalid", "role": "encargado"}
ADMIN = {"id": "m1", "full_name": "Administrador", "institutional_email": "adm@ludica.invalid", "role": "maestro"}


def _row(session, scenario, kind, verdict, transaction, second):
    return {"sesion_id": session, "timestamp": f"2026-11-01T12:{second // 60:02d}:{second % 60:02d}+00:00",
            "estacion": "operario", "tipo_evento": kind, "item_codigo": ITEM, "transaccion_id": transaction,
            "usuario_sim": "u1", "resultado_sistema": verdict, "escenario": scenario}


SYNTHETIC = [
    _row("S1", "B", "order_requested", "ok", "t1", 0),
    _row("S1", "B", "order_claimed", "ok", "t1", 10),
    _row("S1", "B", event_log.READ_REJECTED, "rechazado", "t1", 20),
    _row("S1", "B", event_log.READ_RETRY, "ok", "t1", 25),
    _row("S1", "B", event_log.READ_DUPLICATE, event_log.RESULT_IGNORED, "t1", 26),
    _row("S1", "B", "order_picked", "ok", "t1", 40),
    _row("S1", "B", "order_delivered", "ok", "t1", 60),
    _row("S1", "C", "order_requested", "ok", "t2", 120),
    _row("S1", "C", "order_claimed", "ok", "t2", 125),
    _row("S1", "C", event_log.READ_OK, "ok", "t2", 130),
    _row("S1", "C", "order_picked", "ok", "t2", 135),
]


def test_summary_by_session_and_scenario_on_synthetic_rows():
    summary = {(row["sesion_id"], row["escenario"]): row for row in research_metrics.summarize(SYNTHETIC)}
    b, c = summary[("S1", "B")], summary[("S1", "C")]
    assert (b["registros"], b["registros_correctos"], b["exactitud_registros"]) == (6, 5, 0.8333)
    assert (b["lecturas"], b["lecturas_fallidas"], b["exactitud_lecturas"]) == (2, 1, 0.5)
    assert (b["transacciones"], b["tiempo_picking_promedio_s"], b["tiempo_total_promedio_s"]) == (1, 30.0, 60.0)
    assert (c["lecturas"], c["lecturas_fallidas"], c["exactitud_lecturas"]) == (1, 0, 1.0)
    assert (c["tiempo_picking_promedio_s"], c["tiempo_total_promedio_s"]) == (10.0, "")
    by_scenario = research_metrics.summarize(SYNTHETIC, by_session=False)
    assert [(row["sesion_id"], row["escenario"]) for row in by_scenario] == [("(todas)", "B"), ("(todas)", "C")]


def test_empty_log_gives_no_rows_and_no_division_by_zero():
    assert research_metrics.summarize([]) == []
    only_ignored = [_row("S1", "A", event_log.READ_DUPLICATE, event_log.RESULT_IGNORED, "t9", 0)]
    row = research_metrics.summarize(only_ignored)[0]
    assert row["exactitud_registros"] == "" and row["exactitud_lecturas"] == "" and row["transacciones"] == 0


@pytest.fixture
def lab(storage):
    storage.add_item(ITEM, name="Resistencia (simulada)", quantity=50)
    for person in (STUDENT, OPERATOR, SUPERVISOR, ADMIN):
        storage.users[person["id"]] = {**person, "status": "active"}
    assert options.set_enabled(storage, ADMIN, options.EVENT_LOG, True)[0]
    assert options.set_enabled(storage, ADMIN, options.RESEARCH_METRICS, True)[0]
    storage.set_setting(ludica.SETTING_SESSION_ID, "S1")
    return storage


def _picking(storage, codes):
    request, _, _ = service_requests.submit_request(storage, STUDENT, "product", ITEM, 1)
    assert service_requests.review_request(storage, request["id"], "approved", SUPERVISOR)[0]
    assert dispatch.claim_request(storage, request["id"], OPERATOR)[0]
    assert dispatch.start_picking(storage, request["id"], OPERATOR)[0]
    for code in codes:
        dispatch.register_reading(storage, request["id"], OPERATOR, code, dispatch.METHOD_BARCODE_GUN)
    assert dispatch.confirm_units(storage, request["id"], OPERATOR, 1)[0]


def test_failed_reads_by_scenario_from_the_event_log(lab):
    assert dispatch.set_scenario(lab, ADMIN, "B")[0]
    _picking(lab, ["LUD-999", ITEM])
    assert dispatch.set_scenario(lab, ADMIN, "C")[0]
    _picking(lab, [ITEM])
    events_before = len(lab.trace_events)
    data = research_metrics.report(lab, ADMIN)
    by_scenario = {row["escenario"]: row for row in data["by_scenario"]}
    assert (by_scenario["B"]["lecturas"], by_scenario["B"]["lecturas_fallidas"]) == (2, 1)
    assert (by_scenario["C"]["lecturas"], by_scenario["C"]["lecturas_fallidas"]) == (1, 0)
    assert by_scenario["B"]["transacciones"] == by_scenario["C"]["transacciones"] == 1
    text = research_metrics.csv_bytes(lab, ADMIN).decode("utf-8")
    assert text.splitlines()[0] == ",".join(research_metrics.COLUMNS)
    assert len(lab.trace_events) == events_before                 # solo lectura: no escribe nada


def test_metrics_are_off_by_default_and_admin_only(storage):
    assert not research_metrics.enabled(storage)
    with pytest.raises(PermissionError):
        research_metrics.report(storage, SUPERVISOR)
