"""M5 · lectura robusta (core/reading.py): normalización, anti doble lectura y mensajes accionables."""

from datetime import datetime, timedelta, timezone

import pytest

from core import dispatch, inventory_count, options, picking, reading, service_requests, traceability

ITEM = "LUD-001"
T0 = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
STUDENT = {"id": "s1", "full_name": "Cliente 01", "institutional_email": "c01@ludica.invalid", "role": "estudiante"}
OPERATOR = {"id": "o1", "full_name": "Operario 01", "institutional_email": "o01@ludica.invalid", "role": "operario"}
SUPERVISOR = {"id": "p1", "full_name": "Supervisor 01", "institutional_email": "p01@ludica.invalid", "role": "encargado"}
PROF = {"id": "f1", "full_name": "Profesora", "institutional_email": "f01@ludica.invalid", "role": "profesor"}
ADMIN = {"id": "m1", "full_name": "Administrador", "institutional_email": "adm@ludica.invalid", "role": "maestro"}


@pytest.fixture
def lab(storage):
    storage.add_item(ITEM, name="Resistencia (simulada)", quantity=50)
    storage.add_item("LUD-002", name="LED (simulado)", quantity=50)
    for person in (STUDENT, OPERATOR, SUPERVISOR, PROF, ADMIN):
        storage.users[person["id"]] = {**person, "status": "active"}
    return storage


def _robust(storage):
    assert options.set_enabled(storage, ADMIN, options.ROBUST_READS, True)[0]


def _claimed(storage, quantity=2):
    request, _, _ = service_requests.submit_request(storage, STUDENT, "product", ITEM, quantity)
    assert service_requests.review_request(storage, request["id"], "approved", SUPERVISOR)[0]
    assert dispatch.claim_request(storage, request["id"], OPERATOR)[0]
    assert dispatch.start_picking(storage, request["id"], OPERATOR)[0]
    return request["id"]


def _read(storage, request_id, code, when):
    return dispatch.register_reading(storage, request_id, OPERATOR, code, dispatch.METHOD_BARCODE_GUN, now=when)


# --- Funciones puras ----------------------------------------------------------------------------

def test_normalize_code_cleans_what_the_gun_sends():
    assert reading.normalize_code(" ]C1lud-001\r\n") == "LUD-001"           # prefijo AIM, minúsculas, Enter
    assert reading.normalize_code("*LUD-001*") == "LUD-001"                 # inicio/fin de Code 39
    assert reading.normalize_code("\x02LUD-\t001\x03") == "LUD-001"         # STX/ETX y un Tab en medio
    assert reading.normalize_code("") == "" and reading.normalize_code(None) == ""


def test_double_read_only_inside_the_window():
    last = {"code": "LUD-001", "at": T0.isoformat()}
    assert reading.is_double_read(last, "lud-001", T0 + timedelta(seconds=1), 1.5)
    assert not reading.is_double_read(last, "LUD-001", T0 + timedelta(seconds=2), 1.5)
    assert not reading.is_double_read(last, "LUD-002", T0 + timedelta(seconds=1), 1.5)
    assert not reading.is_double_read(None, "LUD-001", T0, 1.5)


def test_messages_say_what_was_read_and_what_was_expected():
    point = {"key": "estanteria", "title": "Estantería 2", "label_code": "2-0-00-00-000"}
    assert reading.route_mismatch("2-1-00-00-000", point) == (
        "Leíste 2-1-00-00-000 (el Piso 1 de la Estantería 2); la ruta esperaba 2-0-00-00-000: estantería.")
    text = reading.product_mismatch("LUD-002", {"item_id": ITEM, "item_name": "Resistencia"})
    assert text == "Leíste LUD-002; el pedido esperaba LUD-001: producto «Resistencia»."


def test_window_is_admin_only_and_bounded(storage):
    assert reading.window_seconds(storage) == reading.DEFAULT_WINDOW == 1.5
    with pytest.raises(PermissionError):
        reading.set_window(storage, PROF, 2)
    assert not reading.set_window(storage, ADMIN, 50)[0]
    assert reading.set_window(storage, ADMIN, "2,5")[0] and reading.window_seconds(storage) == 2.5


# --- Picking ------------------------------------------------------------------------------------

def test_robust_picking_normalizes_ignores_the_double_read_and_records_it(lab):
    _robust(lab)
    request_id = _claimed(lab)
    ok, message, count = _read(lab, request_id, " ]C1lud-001\r\n", T0)
    assert ok and count == 1
    assert picking.readings("o1", request_id)[0]["code"] == ITEM            # se guarda el código del inventario
    ok, message, count = _read(lab, request_id, "LUD-001", T0 + timedelta(seconds=1))
    assert ok and "repetida ignorada" in message and "1,5 s" in message and count == 1
    ok, message, _ = _read(lab, request_id, "LUD-002", T0 + timedelta(seconds=5))
    assert not ok and message.startswith("Leíste LUD-002; el pedido esperaba LUD-001")
    ok, _, event = dispatch.confirm_units(lab, request_id, OPERATOR, 2)
    assert ok
    duplicates = traceability.event_details(event)["duplicate_reads"]
    assert len(duplicates) == 1 and duplicates[0]["code"] == "LUD-001"


def test_default_picking_reads_exactly_as_before(lab):
    request_id = _claimed(lab)
    ok, message, _ = _read(lab, request_id, " lud-001 ", T0)
    assert not ok and message == "Se leyó lud-001; se esperaba LUD-001."
    _read(lab, request_id, "LUD-002", T0 + timedelta(seconds=1))
    _read(lab, request_id, "LUD-002", T0 + timedelta(seconds=1, milliseconds=200))   # sin ventana: cuentan los dos
    assert len(picking.failed("o1", request_id)) == 3 and picking.duplicates("o1", request_id) == []


# --- Conteo con el teléfono ----------------------------------------------------------------------

def test_robust_count_ignores_a_double_mark_but_keeps_a_record(lab):
    _robust(lab)
    _, _, session = inventory_count.start_session(lab, PROF, "Conteo", now=T0)
    item = lab.get_item(ITEM)
    assert inventory_count.record_mark(lab, session, PROF, ITEM, 3, item=item, now=T0)[0]
    ok, message, event = inventory_count.record_mark(lab, session, PROF, ITEM, 3, item=item,
                                                     now=T0 + timedelta(seconds=1))
    assert ok and "repetida ignorada" in message
    assert traceability.event_details(event)["duplicate"] is True
    assert len(inventory_count.session_marks(lab, session["id"])) == 1
    assert inventory_count.record_mark(lab, session, PROF, ITEM, 4, item=item, now=T0 + timedelta(seconds=1))[0]
    assert len(inventory_count.session_marks(lab, session["id"])) == 2        # otra cantidad: es una corrección


def test_default_count_marks_both(lab):
    _, _, session = inventory_count.start_session(lab, PROF, "Conteo", now=T0)
    for seconds in (0, 1):
        inventory_count.record_mark(lab, session, PROF, ITEM, 3, now=T0 + timedelta(seconds=seconds))
    assert len(inventory_count.session_marks(lab, session["id"])) == 2
