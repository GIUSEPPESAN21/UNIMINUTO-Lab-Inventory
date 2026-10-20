# -*- coding: utf-8 -*-
"""v1.20 · ruta obligatoria del picking (estantería → piso → contenedor → caja → unidades) y llegada por NFC."""

import pytest

from core import dispatch, nfc, permissions, pick_route, picking, service_requests, traceability

SHELF, FLOOR = "2-0-00-00-000", "2-1-00-00-000"
CONTAINER, BOX, ITEM = "2-1-01-00-000", "2-1-01-02-000", "2-1-01-02-005"
STUDENT = {"id": "s1", "full_name": "Ana", "institutional_email": "ana@uniminuto.edu.co", "role": "estudiante"}
OPERATOR = {"id": "o1", "full_name": "Olga", "institutional_email": "olga@uniminuto.edu.co", "role": "operario"}
SUPERVISOR = {"id": "p1", "full_name": "Pablo", "institutional_email": "pablo@uniminuto.edu.co", "role": "encargado"}
ADMIN = {"id": "m1", "full_name": "Maria", "institutional_email": "maria@uniminuto.edu.co", "role": "maestro"}


@pytest.fixture
def lab(storage):
    storage.add_item(SHELF, name="Estantería 2", item_type="location")
    storage.add_item(FLOOR, name="Piso 1", item_type="location", parent_id=SHELF)
    storage.add_item(CONTAINER, name="Electrónica", item_type="master")
    storage.add_item("2-1-01-02-000", name="Caja de resistencias", item_type="child", parent_id=CONTAINER, quantity=9)
    storage.add_item(ITEM, name="Resistencia 220", item_type="child", parent_id=CONTAINER, quantity=100)
    for person in (STUDENT, OPERATOR, SUPERVISOR, ADMIN):
        storage.users[person["id"]] = {**person, "status": "active"}
    return storage


def _claimed(storage, item=ITEM, quantity=2):
    request, _, _ = service_requests.submit_request(storage, STUDENT, "product", item, quantity)
    assert request
    assert service_requests.review_request(storage, request["id"], "approved", SUPERVISOR)[0]
    assert dispatch.claim_request(storage, request["id"], OPERATOR)[0]
    assert dispatch.start_picking(storage, request["id"], OPERATOR)[0]
    return request["id"]


def _read(storage, request_id, code, method="barcode_gun", actor=OPERATOR):
    return dispatch.register_reading(storage, request_id, actor, code, method)


def test_the_route_is_shelf_floor_container_box_before_the_units(lab):
    points = pick_route.checkpoints(lab, ITEM)
    assert [p["key"] for p in points] == ["estanteria", "piso", "contenedor", "caja"]
    assert [p["label_code"] for p in points] == [SHELF, FLOOR, CONTAINER, BOX]


def test_the_box_as_the_product_makes_the_units_the_box_reads(lab):
    assert [p["key"] for p in pick_route.checkpoints(lab, BOX)] == ["estanteria", "piso", "contenedor"]


def test_units_are_refused_until_the_route_is_read_in_order(lab):
    request_id = _claimed(lab)
    ok, message, _ = _read(lab, request_id, ITEM)                    # intenta las unidades de entrada
    assert not ok and "Primero confirma la ruta" in message and SHELF in message
    ok, message, _ = _read(lab, request_id, CONTAINER)               # el contenedor antes que la estantería
    assert not ok and SHELF in message
    for code in (SHELF, FLOOR, CONTAINER):
        assert _read(lab, request_id, code)[0]
    ok, message, _ = _read(lab, request_id, ITEM)                    # falta la caja
    assert not ok and BOX in message
    ok, message, _ = _read(lab, request_id, BOX, "nfc")
    assert ok and "Ruta completa" in message
    ok, message, count = _read(lab, request_id, ITEM)                # la etiqueta de la caja se lee UNA vez
    assert ok and count == 1 and "cuenta las 2 unidad(es)" in message
    assert _read(lab, request_id, ITEM)[2] == 1                      # leerla otra vez no suma unidades
    assert [step["code"] for step in picking.route_steps("o1", request_id)] == [SHELF, FLOOR, CONTAINER, BOX]


def test_picking_cannot_be_completed_without_the_route_and_records_it_when_done(lab):
    request_id = _claimed(lab)
    ok, message, _ = dispatch.complete_picking(lab, request_id, OPERATOR, readings=[ITEM, ITEM])
    assert not ok and "lee la ruta" in message
    ok, message, _ = dispatch.confirm_manual_picking(lab, request_id, OPERATOR, True, "sin lector")
    assert not ok and "lee la ruta" in message                       # la confirmación manual tampoco se salta la ruta
    for code in (SHELF, FLOOR, CONTAINER, BOX, ITEM):
        assert _read(lab, request_id, code)[0]
    ok, message, event = dispatch.complete_picking(lab, request_id, OPERATOR)
    assert ok, message
    details = traceability.event_details(event)
    assert [step["key"] for step in details["route"]] == ["estanteria", "piso", "contenedor", "caja"]
    assert "Ruta leída: estanteria › piso › contenedor › caja" in traceability.describe_event(event)


def test_wrong_reads_become_failed_scans_and_repeated_route_taps_are_ignored(lab):
    request_id = _claimed(lab)
    assert not _read(lab, request_id, "2-1-02-00-000")[0]
    assert picking.failed("o1", request_id)
    assert _read(lab, request_id, SHELF, "nfc")[0]
    before = len(picking.route_steps("o1", request_id))
    ok, message, _ = _read(lab, request_id, SHELF, "nfc")             # el chip se toca dos veces: no avanza ni falla
    assert not ok and "Primero confirma la ruta" in message and FLOOR in message
    for code in (FLOOR, CONTAINER, BOX):
        _read(lab, request_id, code)
    ok, message, _ = _read(lab, request_id, CONTAINER, "nfc")         # ruta completa y se vuelve a tocar el contenedor
    assert ok and "ya estaba confirmado" in message
    assert len(picking.route_steps("o1", request_id)) == before + 3


def test_undo_removes_the_last_unit_and_then_the_last_route_point(lab):
    request_id = _claimed(lab)
    for code in (SHELF, FLOOR, CONTAINER, BOX, ITEM):
        _read(lab, request_id, code)
    dispatch.undo_reading(OPERATOR, request_id)
    assert dispatch.current_readings(OPERATOR, request_id) == [] and len(picking.route_steps("o1", request_id)) == 4
    dispatch.undo_reading(OPERATOR, request_id)
    assert [s["key"] for s in picking.route_steps("o1", request_id)] == ["estanteria", "piso", "contenedor"]


def test_shelf_and_floor_are_only_required_when_registered(lab):
    del lab.items[SHELF], lab.items[FLOOR]
    assert [p["key"] for p in pick_route.checkpoints(lab, ITEM)] == ["contenedor", "caja"]


def test_free_codes_without_route_are_not_asked_for_anything(lab):
    lab.add_item("LIBRE-1", name="Pinza", quantity=3)
    assert pick_route.checkpoints(lab, "LIBRE-1") == []
    request_id = _claimed(lab, "LIBRE-1", 1)
    assert _read(lab, request_id, "LIBRE-1")[0]
    assert dispatch.complete_picking(lab, request_id, OPERATOR)[0]


def test_the_administrator_can_turn_the_requirement_off(lab):
    with pytest.raises(PermissionError):
        pick_route.set_required(lab, SUPERVISOR, False)
    assert pick_route.set_required(lab, ADMIN, False)[0] and not pick_route.required(lab)
    request_id = _claimed(lab)
    assert _read(lab, request_id, ITEM)[0]                            # sin exigencia, las unidades se leen directo
    assert pick_route.set_required(lab, ADMIN, True)[0] and pick_route.required(lab)
    assert permissions.has_cap(ADMIN, permissions.CAP_DISPATCH_SETTINGS)


def test_progress_for_the_panel(lab):
    request_id = _claimed(lab)
    _read(lab, request_id, SHELF)
    route = dispatch.route_progress(lab, OPERATOR, request_id)
    assert route["required"] and route["done"] == 1 and not route["complete"] and route["next"]["key"] == "piso"


# --- NFC: reconoce la llegada ---------------------------------------------------------------

def test_a_chip_on_a_container_shelf_or_floor_says_you_arrived(lab):
    for code, kind, text in ((CONTAINER, "contenedor", "Llegaste al contenedor «Electrónica»"),
                             (SHELF, "estantería", "Llegaste a la estantería «Estantería 2»"),
                             (FLOOR, "piso", "Llegaste al piso «Piso 1»")):
        outcome = nfc.handle_tap(lab, STUDENT, code, secret="")
        assert outcome["ok"] and outcome["arrival"] == kind and outcome["message"].startswith(text), outcome["message"]
    assert "2 producto(s) dentro" in nfc.handle_tap(lab, STUDENT, CONTAINER, secret="", now="2030-01-01T00:00:00+00:00")["message"]
    assert nfc.handle_tap(lab, STUDENT, ITEM, secret="")["arrival"] == ""     # un producto no es un lugar con chip


def test_the_tap_event_remembers_the_arrival_kind(lab):
    outcome = nfc.handle_tap(lab, STUDENT, CONTAINER, secret="")
    assert traceability.event_details(outcome["event"])["arrival"] == "contenedor"


def test_nfc_taps_advance_the_picking_route_from_a_new_tab(lab):
    request_id = _claimed(lab)
    for code in (SHELF, FLOOR, CONTAINER):
        outcome = nfc.handle_tap(lab, OPERATOR, code, secret="")
        ok, message = dispatch.register_active_reading(lab, OPERATOR, code, "nfc", accepted=outcome["ok"])
        assert ok, message
    assert [s["key"] for s in picking.route_steps("o1", request_id)] == ["estanteria", "piso", "contenedor"]
    assert all(s["method"] == "nfc" for s in picking.route_steps("o1", request_id))


# --- las unidades no llevan código: se lee la etiqueta de la caja una vez y se cuentan -------------

def _ready_to_count(lab, quantity=2):
    request_id = _claimed(lab, ITEM, quantity)
    for code in (SHELF, FLOOR, CONTAINER, BOX, ITEM):
        assert _read(lab, request_id, code)[0]
    return request_id


def test_units_are_counted_not_scanned_and_each_one_is_recorded_as_counted(lab):
    request_id = _ready_to_count(lab, 3)
    assert len(dispatch.current_readings(OPERATOR, request_id)) == 1            # solo la etiqueta de la caja
    ok, message, event = dispatch.confirm_units(lab, request_id, OPERATOR, 3, "sin novedad")
    assert ok, message
    details = traceability.event_details(event)
    assert [r["method"] for r in details["readings"]] == ["count"] * 3
    assert all(r["code"] == ITEM for r in details["readings"])
    assert details["label_read"]["code"] == ITEM and details["counted_units"] == 3
    assert details["method"] == "count" and details["count_note"] == "sin novedad"
    text = traceability.describe_event(event)
    assert "3 unidad(es) con conteo de las unidades" in text and "Etiqueta de la caja leída con" in text
    assert lab.get_available_quantity(ITEM) == 97


def test_the_count_must_match_the_order_and_needs_the_label_first(lab):
    request_id = _claimed(lab, ITEM, 2)
    ok, message, _ = dispatch.confirm_units(lab, request_id, OPERATOR, 2)
    assert not ok and "lee la ruta" in message                                  # la ruta primero
    for code in (SHELF, FLOOR, CONTAINER, BOX):
        _read(lab, request_id, code)
    ok, message, _ = dispatch.confirm_units(lab, request_id, OPERATOR, 2)
    assert not ok and "etiqueta de la caja" in message                          # luego la etiqueta
    _read(lab, request_id, ITEM)
    for bad in (1, 3, 0, "x", None):
        ok, message, _ = dispatch.confirm_units(lab, request_id, OPERATOR, bad)
        assert not ok, bad
    assert "pedido es de 2" in dispatch.confirm_units(lab, request_id, OPERATOR, 5)[1]
    assert dispatch.confirm_units(lab, request_id, OPERATOR, 2)[0]


def test_the_count_method_cannot_be_used_to_fake_a_label_read(lab):
    request_id = _claimed(lab, ITEM, 1)
    for code in (SHELF, FLOOR, CONTAINER, BOX):
        _read(lab, request_id, code)
    ok, message, _ = _read(lab, request_id, ITEM, "count")
    assert not ok and "solo para confirmar" in message
    assert dispatch.current_readings(OPERATOR, request_id) == []
    with pytest.raises(PermissionError):
        dispatch.confirm_units(lab, request_id, STUDENT, 1)
