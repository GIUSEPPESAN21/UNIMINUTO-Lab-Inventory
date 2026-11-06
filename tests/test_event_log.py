"""M3 · registro de eventos único, ordenado y exportable (core/event_log.py)."""

import csv
import io
from datetime import datetime, timedelta, timezone

import pytest

from core import dispatch, event_log, ludica, picking, service_requests, traceability

ITEM = "LUD-001"
STUDENT = {"id": "s1", "full_name": "Cliente 01", "institutional_email": "c01@ludica.invalid", "role": "estudiante"}
OPERATOR = {"id": "o1", "full_name": "Operario 01", "institutional_email": "o01@ludica.invalid", "role": "operario"}
SUPERVISOR = {"id": "p1", "full_name": "Supervisor 01", "institutional_email": "p01@ludica.invalid", "role": "encargado"}
ADMIN = {"id": "m1", "full_name": "Administrador", "institutional_email": "adm@ludica.invalid", "role": "maestro"}
LEGACY_TIME_FIELDS = ["request_id", "stage", "requested_at", "claim_seconds", "picking_seconds",
                      "delivery_seconds", "picker_role"]


@pytest.fixture
def lab(storage):
    storage.add_item(ITEM, name="Resistencia 220 (simulada)", quantity=50)
    for person in (STUDENT, OPERATOR, SUPERVISOR, ADMIN):
        storage.users[person["id"]] = {**person, "status": "active"}
    return storage


def _enable(storage, session="S1", scenario="C"):
    assert event_log.set_enabled(storage, ADMIN, True)[0]
    storage.set_setting(ludica.SETTING_SESSION_ID, session)
    assert dispatch.set_scenario(storage, ADMIN, scenario)[0]


def _claimed(storage, quantity=2, created_at=None):
    request, _, _ = service_requests.submit_request(storage, STUDENT, "product", ITEM, quantity)
    if created_at:
        storage.service_requests[request["id"]]["created_at"] = created_at
    assert service_requests.review_request(storage, request["id"], "approved", SUPERVISOR)[0]
    assert dispatch.claim_request(storage, request["id"], OPERATOR)[0]
    assert dispatch.start_picking(storage, request["id"], OPERATOR)[0]
    return request["id"]


def _read(storage, request_id, code):
    return dispatch.register_reading(storage, request_id, OPERATOR, code, dispatch.METHOD_BARCODE_GUN)


def _picked_event(storage, request_id):
    return next(e for e in storage.get_trace_events(request_id=request_id)
                if e["event_type"] == traceability.EVENT_ORDER_PICKED)


# --- Valor por defecto: nada cambia ------------------------------------------------------------

def test_default_off_writes_nothing_new_and_keeps_both_csv_formats(lab):
    assert not event_log.enabled(lab) and event_log.stamp_fields(lab) == {}
    request_id = _claimed(lab)
    assert not _read(lab, request_id, "LUD-999")[0]
    assert _read(lab, request_id, ITEM)[0]
    assert dispatch.confirm_units(lab, request_id, OPERATOR, 2)[0]
    assert all(e["ludica_session"] == "" and e["scenario"] == "" for e in lab.trace_events)
    assert "read_log" not in traceability.event_details(_picked_event(lab, request_id))
    failed = next(e for e in lab.trace_events if e["event_type"] == traceability.EVENT_ORDER_SCAN_FAILED)
    assert "in_read_log" not in traceability.event_details(failed)
    header = dispatch.dispatch_csv(lab, SUPERVISOR).decode("utf-8-sig").splitlines()[0]
    assert header.split(",") == LEGACY_TIME_FIELDS
    assert ludica.events_csv(lab, SUPERVISOR).decode("utf-8").splitlines()[0] == ",".join(ludica.COLUMNS)


# --- Sello de sesión y escenario ---------------------------------------------------------------

def test_events_and_requests_are_stamped_when_enabled(lab):
    _enable(lab, "S1", "C")
    request_id = _claimed(lab)
    assert lab.service_requests[request_id]["ludica_session"] == "S1"
    assert lab.service_requests[request_id]["scenario"] == "C"
    events = lab.get_trace_events(request_id=request_id)
    assert events and all(e["ludica_session"] == "S1" and e["scenario"] == "C" for e in events)


def test_only_the_admin_turns_the_log_on(storage):
    with pytest.raises(PermissionError):
        event_log.set_enabled(storage, SUPERVISOR, True)
    assert not event_log.enabled(storage)


# --- Lecturas del picking -----------------------------------------------------------------------

def test_each_reading_is_logged_once_with_server_time(lab):
    _enable(lab)
    request_id = _claimed(lab)
    assert not _read(lab, request_id, "LUD-999")[0]        # incorrecta
    assert _read(lab, request_id, ITEM)[0]                  # reintento correcto
    assert _read(lab, request_id, ITEM)[0]                  # repetida (ya estaba leída)
    assert dispatch.confirm_units(lab, request_id, OPERATOR, 2)[0]
    log = traceability.event_details(_picked_event(lab, request_id))["read_log"]
    assert [entry["result"] for entry in log] == ["rechazado", "ok", "repetida"]
    assert all(entry["at"] and entry["scenario"] == "C" and entry["ludica_session"] == "S1" for entry in log)
    kinds = [row["tipo_evento"] for row in event_log.rows(lab) if row["transaccion_id"] == ludica.pseudonym(request_id)]
    assert kinds.count(event_log.READ_REJECTED) == 1 and kinds.count(event_log.READ_RETRY) == 1
    assert kinds.count(event_log.READ_DUPLICATE) == 1
    assert traceability.EVENT_ORDER_SCAN_FAILED not in kinds    # el fallo ya está en read_log: no se duplica
    assert kinds.index(event_log.READ_REJECTED) < kinds.index(traceability.EVENT_ORDER_PICKED)


def test_release_and_closed_panel_keep_the_reads(lab):
    _enable(lab)
    request_id = _claimed(lab)
    assert not _read(lab, request_id, "LUD-999")[0]
    dispatch.abandon_picking(OPERATOR, request_id, lab)                 # cerrar el panel no borra el registro
    assert picking.active_request("o1") == "" and len(picking.read_log("o1", request_id)) == 1
    assert dispatch.release_request(lab, request_id, OPERATOR, "Me llamaron")[0]
    released = next(e for e in lab.get_trace_events(request_id=request_id)
                    if e["event_type"] == traceability.EVENT_ORDER_RELEASED)
    assert [entry["code"] for entry in traceability.event_details(released)["read_log"]] == ["LUD-999"]
    assert picking.read_log("o1", request_id) == []


def test_undo_is_logged(lab):
    _enable(lab)
    request_id = _claimed(lab)
    assert _read(lab, request_id, ITEM)[0]
    dispatch.undo_reading(OPERATOR, request_id, lab)
    assert [entry["result"] for entry in picking.read_log("o1", request_id)] == ["ok", "deshecha"]


def test_manual_confirmation_is_its_own_row(lab):
    _enable(lab)
    request_id = _claimed(lab)
    assert dispatch.confirm_manual_picking(lab, request_id, OPERATOR, True, "pistola sin batería")[0]
    kinds = [row["tipo_evento"] for row in event_log.rows(lab)]
    assert event_log.MANUAL_CONFIRMATION in kinds


# --- Exportación y tiempos ---------------------------------------------------------------------

def test_times_derived_from_the_log_match_the_tiempos_report(lab):
    _enable(lab)
    created = (datetime.now(timezone.utc) - timedelta(seconds=42, milliseconds=700)).isoformat()
    request_id = _claimed(lab, created_at=created)
    assert _read(lab, request_id, ITEM)[0]
    assert dispatch.confirm_units(lab, request_id, OPERATOR, 2)[0]
    assert dispatch.hand_over(lab, request_id, SUPERVISOR)[0]
    report = {row["request_id"]: row for row in dispatch.dispatch_metrics(lab, SUPERVISOR)}[request_id]
    derived = event_log.times_from_rows(event_log.rows(lab))[ludica.pseudonym(request_id)]
    for column in ("claim_seconds", "picking_seconds", "delivery_seconds"):
        assert derived[column] == report[column] != ""
    data = list(csv.DictReader(io.StringIO(dispatch.dispatch_csv(lab, SUPERVISOR).decode("utf-8-sig"))))
    assert list(data[0])[:7] == LEGACY_TIME_FIELDS and list(data[0])[7:] == list(event_log.TIME_COLUMNS)
    assert data[0]["transaccion_id"] == ludica.pseudonym(request_id) and data[0]["escenario"] == "C"


def test_csv_has_the_columns_and_filters_by_session(lab):
    _enable(lab, "S1")
    _claimed(lab)
    lab.set_setting(ludica.SETTING_SESSION_ID, "S2")
    _claimed(lab)
    text = event_log.csv_bytes(lab, SUPERVISOR, "S1").decode("utf-8")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert text.splitlines()[0] == ",".join(event_log.COLUMNS)
    assert rows and {row["sesion_id"] for row in rows} == {"S1"}
    assert event_log.sessions(lab) == ["S1", "S2"]
    stamps = [row["timestamp"] for row in event_log.rows(lab)]
    earliest = datetime.min.replace(tzinfo=timezone.utc)
    assert stamps == sorted(stamps, key=lambda value: traceability.to_datetime(value) or earliest)


def test_csv_requires_the_report_capability(lab):
    with pytest.raises(PermissionError):
        event_log.csv_bytes(lab, STUDENT)


def test_bogota_time_is_shown_with_its_zone():
    assert event_log.bogota_text("2026-10-09T15:00:00.123+00:00") == "2026-10-09 10:00:00.123 (America/Bogota)"
    assert event_log.bogota_text("") == ""
