"""M6 · conteo con el teléfono: continuidad (retomar tras recargar) y control por ubicación."""

from datetime import datetime, timedelta, timezone

import pytest

from core import inventory_count as counting
from core import nfc, options, traceability

NOW = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
ELECTRONICS = "2-1-01-00-000"
ELECTRONICS_LABEL = "Electrónica (2-1-01-00-000)"


@pytest.fixture
def lab(storage):
    storage.add_item(ELECTRONICS, name="Electrónica", item_type="master")
    storage.add_item("2-1-01-01-001", name="Arduino UNO", item_type="child", parent_id=ELECTRONICS, quantity=5)
    storage.add_item("2-1-01-01-002", name="Protoboard", item_type="child", parent_id=ELECTRONICS, quantity=2)
    storage.add_item("3-2-04-00-000", name="Lego", item_type="master")
    storage.add_item("3-2-04-01-001", name="Pines 4x2", item_type="child", parent_id="3-2-04-00-000", quantity=40)
    storage.add_item("LAB-MIC-01", name="Microscopio", quantity=1)
    return {
        "storage": storage,
        "student": storage.add_user("ana@ludica.invalid", full_name="Ana", role="estudiante"),
        "prof": storage.add_user("prof@ludica.invalid", full_name="Profe", role="profesor"),
        "attendant": storage.add_user("enc@ludica.invalid", full_name="Encargada", role="encargado"),
        "master": storage.add_user("maestro@ludica.invalid", full_name="Maestro", role="maestro"),
    }


def _enable(lab):
    assert options.set_enabled(lab["storage"], lab["master"], options.COUNT_BY_LOCATION, True)[0]


def _progress(lab, session):
    storage = lab["storage"]
    items = [dict(item) for item in storage.items.values()]
    return counting.location_progress(session, items, counting.session_marks(storage, session["id"]),
                                      counting.location_states(storage, session["id"]))


def _start(lab, scope=None):
    ok, _, session = counting.start_session(lab["storage"], lab["prof"], "Conteo", scope, now=NOW)
    assert ok
    return session


def test_default_off_counts_as_before(lab):
    storage, prof = lab["storage"], lab["prof"]
    session = _start(lab)
    assert not counting.finish_location(storage, session, prof, ELECTRONICS, ELECTRONICS_LABEL)[0]
    assert counting.location_blocked(storage, session, storage.get_item("2-1-01-01-002")) == ""
    for minute in (0, 1):
        assert counting.record_mark(storage, session, prof, "2-1-01-01-001", 5, now=NOW + timedelta(minutes=minute))[0]
    outcome = nfc.handle_tap(storage, prof, "2-1-01-01-002", secret="", now=NOW + timedelta(minutes=2))
    assert outcome["ok"] and outcome["target"] == nfc.TARGET_COUNT


def test_progress_by_location_counts_done_and_pending(lab):
    _enable(lab)
    storage, prof = lab["storage"], lab["prof"]
    session = _start(lab)
    progress = _progress(lab, session)
    assert [row["location"] for row in progress["rows"]] == [ELECTRONICS_LABEL, "Lego (3-2-04-00-000)",
                                                             counting.NO_PLACE]
    assert (progress["total"], progress["done"], progress["pending"]) == (3, 0, 3)
    counting.record_mark(storage, session, prof, "2-1-01-01-001", 5, now=NOW)
    assert _progress(lab, session)["rows"][0]["status"] == counting.STATUS_IN_PROGRESS
    ok, message, _ = counting.finish_location(storage, session, prof, ELECTRONICS, ELECTRONICS_LABEL)
    assert ok and "quedó contada" in message
    progress = _progress(lab, session)
    assert (progress["done"], progress["pending"]) == (1, 2)
    assert progress["rows"][0]["status"] == counting.STATUS_DONE and progress["rows"][0]["done_by"] == "Profe"
    assert not counting.finish_location(storage, session, prof, ELECTRONICS, ELECTRONICS_LABEL)[0]   # dos veces no


def test_scope_limits_the_locations(lab):
    _enable(lab)
    session = _start(lab, {"kind": "container", "id": ELECTRONICS, "name": "Electrónica"})
    assert _progress(lab, session)["total"] == 1


def test_a_counted_location_needs_a_supervisor_to_be_counted_again(lab):
    _enable(lab)
    storage, prof, attendant = lab["storage"], lab["prof"], lab["attendant"]
    session = _start(lab)
    counting.record_mark(storage, session, prof, "2-1-01-01-001", 5, now=NOW)
    assert counting.finish_location(storage, session, prof, ELECTRONICS, ELECTRONICS_LABEL)[0]
    ok, message, _ = counting.record_mark(storage, session, prof, "2-1-01-01-002", 2, now=NOW + timedelta(minutes=1))
    assert not ok and "ya se contó" in message
    taps_before = len(storage.get_trace_events(event_type=traceability.EVENT_NFC_TAP))
    outcome = nfc.handle_tap(storage, prof, "2-1-01-01-002", secret="", now=NOW + timedelta(minutes=2))
    assert outcome["tone"] == "warning" and "ya se contó" in outcome["message"]
    assert len(storage.get_trace_events(event_type=traceability.EVENT_NFC_TAP)) == taps_before
    assert not counting.authorize_recount(storage, session, lab["student"], ELECTRONICS, ELECTRONICS_LABEL, "x")[0]
    ok, message, _ = counting.authorize_recount(storage, session, attendant, ELECTRONICS, ELECTRONICS_LABEL, "")
    assert not ok and "motivo" in message
    assert counting.authorize_recount(storage, session, attendant, ELECTRONICS, ELECTRONICS_LABEL,
                                      "se cayó una caja")[0]
    assert counting.record_mark(storage, session, prof, "2-1-01-01-002", 2, now=NOW + timedelta(minutes=3))[0]
    assert counting.finish_location(storage, session, prof, ELECTRONICS, ELECTRONICS_LABEL)[0]
    state = counting.location_states(storage, session["id"])[ELECTRONICS]
    assert state["done"] and state["recounts"] == 1 and state["authorized_by"] == "Encargada"
    assert state["reason"] == "se cayó una caja"


def test_resume_point_comes_from_the_database(lab):
    _enable(lab)
    storage, prof = lab["storage"], lab["prof"]
    session = _start(lab)
    counting.record_mark(storage, session, prof, "2-1-01-01-001", 5, now=NOW)
    counting.record_mark(storage, session, prof, "3-2-04-01-001", None, now=NOW + timedelta(minutes=1))
    # Una recarga crea una sesión de Streamlit nueva: lo único que queda es la base.
    point = counting.resume_point(storage, counting.open_session(storage), prof)
    assert point["code"] == "3-2-04-01-001" and point["method"] == counting.METHOD_CODE
    assert traceability.to_datetime(point["at"]) == NOW + timedelta(minutes=1)
    assert counting.resume_point(storage, session, lab["attendant"]) is None
