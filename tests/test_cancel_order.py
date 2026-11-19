# -*- coding: utf-8 -*-
"""v1.23 · cancelar un pedido en Despacho (y en Solicitudes).

- Solo ``cancel_order`` (profesor y administrador) cancela; el cliente, el operario y el encargado reciben rechazo en la
  vista (no ven el botón) y en la capa de datos (dispatch, service_requests y transition_request).
- Se cancela en cada etapa hasta «Recibida · por entregar», con motivo obligatorio; desde «Entregada» no (Devoluciones).
- Tras el picking, la misma escritura cierra el préstamo y las unidades vuelven al estante.
- El pedido cancelado sale de las listas, de los promedios y de las métricas, pero sigue en los CSV.
- Aviso de «Posible duplicado» (mismo cliente y producto dentro de la ventana de Ajustes).
"""

import csv
import io
from datetime import datetime, timedelta, timezone

import pytest

from core import dispatch, event_log, ludica, permissions, research_metrics, service_requests, traceability, tracker

ITEM = "2-1-04-10-000"
OTHER_ITEM = "2-1-04-11-000"
STUDENT = {"id": "s1", "full_name": "Ana", "institutional_email": "ana@uniminuto.edu.co", "role": "estudiante"}
OPERATOR = {"id": "o1", "full_name": "Olga", "institutional_email": "olga@uniminuto.edu.co", "role": "operario"}
ATTENDANT = {"id": "e1", "full_name": "Elena", "institutional_email": "elena@uniminuto.edu.co", "role": "encargado"}
PROFESSOR = {"id": "p1", "full_name": "Carlos", "institutional_email": "carlos@uniminuto.edu.co", "role": "profesor"}
ADMIN = {"id": "m1", "full_name": "Maria", "institutional_email": "maria@uniminuto.edu.co", "role": "maestro"}
DENIED = (STUDENT, OPERATOR, ATTENDANT)
ALLOWED = (PROFESSOR, ADMIN)

STEPS = {
    traceability.STAGE_REQUESTED: [],
    traceability.STAGE_APPROVED: ["approve"],
    traceability.STAGE_CLAIMED: ["approve", "claim"],
    traceability.STAGE_PICKED: ["approve", "claim", "pick"],
    traceability.STAGE_RECEIVED: ["approve", "claim", "pick", "receive"],
    traceability.STAGE_DELIVERED: ["approve", "claim", "pick", "receive", "deliver"],
    traceability.STAGE_CONFIRMED: ["approve", "claim", "pick", "receive", "deliver", "confirm"],
}


@pytest.fixture
def lab(storage):
    storage.add_item(ITEM, name="Multímetro", quantity=10)
    storage.add_item(OTHER_ITEM, name="Osciloscopio", quantity=10)
    for person in (STUDENT, OPERATOR, ATTENDANT, PROFESSOR, ADMIN):
        storage.users[person["id"]] = {**person, "status": "active"}
    storage.set_setting("picking_route_required", "0")      # aquí se prueba la cancelación, no la ruta (test_pick_route)
    return storage


def _advance(storage, request_id, steps):
    actions = {
        "approve": lambda: service_requests.review_request(storage, request_id, "approved", ATTENDANT)[0],
        "claim": lambda: dispatch.claim_request(storage, request_id, OPERATOR)[0],
        "pick": lambda: dispatch.complete_picking(
            storage, request_id, OPERATOR,
            readings=[{"code": ITEM, "method": "manual"}] * int(storage.get_service_request(request_id)["quantity"]))[0],
        "receive": lambda: dispatch.receive_request(storage, request_id, ATTENDANT)[0],
        "deliver": lambda: dispatch.deliver_request(storage, request_id, ATTENDANT)[0],
        "confirm": lambda: dispatch.confirm_client_receipt(storage, request_id, STUDENT)[0],
        "rework": lambda: dispatch.rework_request(storage, request_id, ATTENDANT, "falta revisar")[0],
    }
    for step in steps:
        assert actions[step](), step


def _order(storage, stage, quantity=2):
    row, _, _ = service_requests.submit_request(storage, STUDENT, "product", ITEM, quantity)
    _advance(storage, row["id"], STEPS[stage])
    assert _stage(storage, row["id"]) == stage
    return row["id"]


def _stage(storage, request_id):
    return traceability.request_stage_key(storage.get_service_request(request_id),
                                          storage.get_trace_events(request_id=request_id))


def _cancel(storage, request_id, actor=PROFESSOR, reason=dispatch.CANCEL_CLIENT_ERROR, text="", original=""):
    return dispatch.cancel_request(storage, request_id, actor, reason, text, original)


# --- permisos: solo profesor y administrador ---------------------------------------------------

def test_only_professor_and_admin_hold_the_cancel_capability():
    holders = {role for role in permissions.ROLES if permissions.has_cap({"role": role}, permissions.CAP_CANCEL_ORDER)}
    assert holders == {permissions.ROLE_PROFESSOR, permissions.ROLE_MASTER}
    assert permissions.roles_with(permissions.CAP_CANCEL_ORDER) == (permissions.ROLE_PROFESSOR, permissions.ROLE_MASTER)
    assert not hasattr(permissions, "CAP_CANCEL_OWN_REQUEST")             # la capacidad del cliente ya no existe
    assert [dispatch.can_cancel(person) for person in DENIED + ALLOWED] == [False, False, False, True, True]


@pytest.mark.parametrize("person", DENIED, ids=lambda p: p["role"])
def test_the_data_layer_refuses_whoever_lacks_the_capability(lab, person):
    request_id = _order(lab, traceability.STAGE_REQUESTED)
    with pytest.raises(PermissionError):
        _cancel(lab, request_id, person)
    with pytest.raises(PermissionError):
        dispatch.cancel_order(lab, "abc12345", person, dispatch.CANCEL_CLIENT_ERROR)
    ok, message = service_requests.cancel_request(lab, request_id, person, dispatch.CANCEL_CLIENT_ERROR)
    assert not ok and "permiso" in message
    assert not service_requests.cancel_order(lab, "abc12345", person, dispatch.CANCEL_CLIENT_ERROR)[0]
    # Ni llamando directamente a la capa de datos (sin pasar por dispatch): queda el intento auditado.
    ok, message, row = lab.transition_request(request_id, traceability.EVENT_ORDER_CANCELLED, person,
                                              {"reason_code": dispatch.CANCEL_CLIENT_ERROR})
    assert not ok and "permiso" in message and row["event_type"] == traceability.EVENT_ORDER_ACTION_FAILED
    assert lab.get_service_request(request_id)["status"] == "pending"


def test_the_owner_no_longer_cancels_even_their_own_request(lab):
    request_id = _order(lab, traceability.STAGE_REQUESTED)
    ok, message = service_requests.cancel_request(lab, request_id, STUDENT, dispatch.CANCEL_CLIENT_ERROR)
    assert not ok and "profesor" in message
    assert _stage(lab, request_id) == traceability.STAGE_REQUESTED


def test_approve_no_longer_means_cancel(lab):
    """El encargado aprueba y rechaza, pero ya no cancela (antes bastaba con `approve`)."""
    request_id = _order(lab, traceability.STAGE_APPROVED)
    assert permissions.has_cap(ATTENDANT, permissions.CAP_APPROVE)
    ok, message, _ = lab.transition_request(request_id, traceability.EVENT_ORDER_CANCELLED, ATTENDANT,
                                            {"reason_code": dispatch.CANCEL_NO_STOCK})
    assert not ok and "permiso" in message


# --- cancelar en cada etapa permitida ------------------------------------------------------------

@pytest.mark.parametrize("stage", sorted(traceability.CANCELLABLE_STAGES))
@pytest.mark.parametrize("person", ALLOWED, ids=lambda p: p["role"])
def test_cancel_in_every_stage_up_to_received(lab, stage, person):
    request_id = _order(lab, stage)
    ok, message, event = _cancel(lab, request_id, person, dispatch.CANCEL_NO_STOCK, "sin unidades en buen estado")
    assert ok, message
    assert _stage(lab, request_id) == traceability.STAGE_CANCELLED
    assert lab.get_service_request(request_id)["status"] == "cancelled"
    details = traceability.event_details(event)
    assert event["event_type"] == traceability.EVENT_ORDER_CANCELLED
    assert event["actor_name"] == person["full_name"] and event["actor_role"] == person["role"]   # quién y su rol
    assert event["created_at"]                                                                       # cuándo
    assert details["from_stage"] == stage and details["from_stage_label"] == traceability.STAGES[stage][0]
    assert details["reason_code"] == dispatch.CANCEL_NO_STOCK and details["reason"] == "Sin stock (sin unidades en buen estado)"
    assert all(o["request"]["id"] != request_id for o in dispatch.open_orders(lab))                # sale de las listas
    assert lab.get_available_quantity(ITEM) == 10                                                  # nada queda fuera


@pytest.mark.parametrize("stage", [traceability.STAGE_DELIVERED, traceability.STAGE_CONFIRMED])
def test_delivered_orders_are_not_cancelled_use_returns(lab, stage):
    request_id = _order(lab, stage)
    before = (lab.get_service_request(request_id)["status"], lab.get_available_quantity(ITEM))
    ok, message, row = _cancel(lab, request_id)
    assert not ok and "Devoluciones" in message
    assert row["event_type"] == traceability.EVENT_ORDER_ACTION_FAILED                    # auditado
    assert (lab.get_service_request(request_id)["status"], lab.get_available_quantity(ITEM)) == before
    assert _stage(lab, request_id) == stage
    preview = dispatch.cancel_preview(lab, [request_id])[0]
    assert not preview["can_cancel"] and "Devoluciones" in preview["effect"]


def test_returned_or_already_cancelled_orders_cannot_be_cancelled_again(lab):
    request_id = _order(lab, traceability.STAGE_APPROVED)
    assert _cancel(lab, request_id)[0]
    ok, message, _ = _cancel(lab, request_id)
    assert not ok and "Cancelada" in message


def test_the_reason_is_mandatory_and_other_needs_a_text(lab):
    request_id = _order(lab, traceability.STAGE_APPROVED)
    assert "motivo" in _cancel(lab, request_id, reason="")[1]
    assert "motivo" in _cancel(lab, request_id, reason="porque sí")[1]
    ok, message, _ = _cancel(lab, request_id, reason=dispatch.CANCEL_OTHER, text="   ")
    assert not ok and "Otro" in message
    ok, _, event = _cancel(lab, request_id, reason=dispatch.CANCEL_OTHER, text="El curso se canceló")
    assert ok and traceability.event_details(event)["reason"] == "El curso se canceló"


# --- préstamo: cerrar tras el picking --------------------------------------------------------------

@pytest.mark.parametrize("stage", [traceability.STAGE_PICKED, traceability.STAGE_RECEIVED])
def test_after_picking_the_cancel_closes_the_loan_in_the_same_write(lab, stage):
    request_id = _order(lab, stage, quantity=3)
    loan = next(l for l in lab.loans.values() if l["request_id"] == request_id)
    assert loan["status"] == "out" and lab.get_available_quantity(ITEM) == 7
    preview = dispatch.cancel_preview(lab, [request_id])[0]
    assert preview["can_cancel"] and preview["units_back"] == 3 and "vuelven al estante" in preview["effect"]
    ok, message, event = _cancel(lab, request_id)
    assert ok and "3 unidad(es) vuelven al estante" in message
    assert lab.loans[loan["id"]]["status"] == "returned"
    assert lab.get_available_quantity(ITEM) == 10
    details = traceability.event_details(event)
    assert details["closed_loan_id"] == loan["id"] == event["loan_id"] and details["returned_units"] == 3
    assert "Se cerró el préstamo: 3 unidad(es) volvieron al estante" in traceability.describe_event(event)


def test_a_rework_keeps_the_loan_open_and_the_cancel_still_closes_it(lab):
    request_id = _order(lab, traceability.STAGE_RECEIVED, quantity=1)
    _advance(lab, request_id, ["rework"])                     # vuelve a «Tomada · en picking» con el préstamo abierto
    assert _stage(lab, request_id) == traceability.STAGE_CLAIMED
    ok, _, event = _cancel(lab, request_id)
    assert ok and traceability.event_details(event)["returned_units"] == 1
    assert lab.get_available_quantity(ITEM) == 10 and not lab.get_open_loans_for_item(ITEM)


def test_before_picking_there_is_no_loan_to_close(lab):
    request_id = _order(lab, traceability.STAGE_CLAIMED)
    preview = dispatch.cancel_preview(lab, [request_id])[0]
    assert preview["units_back"] == 0 and "panel" in preview["effect"]
    ok, message, event = _cancel(lab, request_id)
    assert ok and "préstamo" not in message and "closed_loan_id" not in traceability.event_details(event)


def test_the_real_storage_closes_the_loan_and_logs_the_reentry():
    from core.storage import LabStorage

    storage = LabStorage()
    student = storage.create_user("Ana", "ana@uniminuto.edu.co", "x", "estudiante")
    attendant = storage.create_user("Elena", "elena@uniminuto.edu.co", "x", "encargado")
    professor = storage.create_user("Carlos", "carlos@uniminuto.edu.co", "x", "profesor")
    storage.save_item({"name": "Multímetro", "item_type": "standalone", "quantity": 5}, ITEM, is_new=True,
                      actor_email="seed")
    storage.set_setting("picking_route_required", "0", "seed")
    row, _, _ = service_requests.submit_request(storage, student, "product", ITEM, 2)
    assert service_requests.review_request(storage, row["id"], "approved", attendant)[0]
    assert dispatch.claim_request(storage, row["id"], attendant)[0]
    assert dispatch.complete_picking(storage, row["id"], attendant, readings=[{"code": ITEM, "method": "manual"}] * 2)[0]
    assert storage.get_available_quantity(ITEM) == 3
    ok, _, event = dispatch.cancel_request(storage, row["id"], professor, dispatch.CANCEL_CLIENT_ERROR)
    assert ok and storage.get_available_quantity(ITEM) == 5
    assert storage.get_service_request(row["id"])["status"] == "cancelled"
    assert storage.get_service_request(row["id"])["review_notes"] == "Error del cliente"
    assert not [loan for loan in storage.get_all_loans(status="out") if loan.get("request_id") == row["id"]]
    history = storage.get_item_history(ITEM)
    assert any("Pedido cancelado: unidades devueltas al estante" in str(h.get("details")) for h in history)
    assert traceability.event_details(event)["closed_loan_id"]


def test_the_picker_panel_learns_the_order_was_cancelled(lab):
    from core import picking

    request_id = _order(lab, traceability.STAGE_CLAIMED)
    assert dispatch.start_picking(lab, request_id, OPERATOR)[0]
    assert _cancel(lab, request_id)[0]
    ok, message, _ = dispatch.register_reading(lab, request_id, OPERATOR, ITEM, "manual")
    assert not ok and "ya no está" in message                                  # una lectura tardía no suma
    assert not dispatch.complete_picking(lab, request_id, OPERATOR, readings=[{"code": ITEM, "method": "manual"}] * 2)[0]
    assert picking.active_request(OPERATOR["id"]) == request_id               # el panel lo cierra al recargar


# --- duplicado: pedido original -----------------------------------------------------------------

def test_a_duplicate_needs_an_original_of_the_same_client_and_product(lab):
    original = _order(lab, traceability.STAGE_APPROVED)
    duplicate = _order(lab, traceability.STAGE_APPROVED)
    other_product, _, _ = service_requests.submit_request(lab, STUDENT, "product", OTHER_ITEM, 1)
    someone = {"id": "s2", "full_name": "Luis", "institutional_email": "luis@uniminuto.edu.co", "role": "estudiante"}
    lab.users["s2"] = {**someone, "status": "active"}
    foreign, _, _ = service_requests.submit_request(lab, someone, "product", ITEM, 1)
    assert "Elige el pedido original" in _cancel(lab, duplicate, reason=dispatch.CANCEL_DUPLICATE)[1]
    assert "sí mismo" in _cancel(lab, duplicate, reason=dispatch.CANCEL_DUPLICATE, original=duplicate)[1]
    for wrong in (other_product["id"], foreign["id"]):
        assert "mismo cliente y del mismo producto" in _cancel(
            lab, duplicate, reason=dispatch.CANCEL_DUPLICATE, original=wrong)[1]
    assert [row["id"] for row in dispatch.duplicate_candidates(lab, lab.get_service_request(duplicate))] == [original]
    ok, _, event = _cancel(lab, duplicate, reason=dispatch.CANCEL_DUPLICATE, original=original)
    assert ok
    details = traceability.event_details(event)
    assert details["original_request_id"] == original
    assert details["reason"] == f"Pedido duplicado de {traceability.short_id(original)}"
    assert traceability.describe_event(event) == (
        f"Canceló el pedido en la cola, antes de tomarlo: pedido duplicado de {traceability.short_id(original)}")
    # El original no se puede «cancelar como duplicado» de uno ya cancelado.
    assert "cancelado o rechazado" in _cancel(lab, original, reason=dispatch.CANCEL_DUPLICATE, original=duplicate)[1]


def test_whole_order_cancel_maps_each_line_and_skips_delivered_ones(lab):
    first, _, _ = service_requests.submit_order(lab, STUDENT, [{"item_id": ITEM, "quantity": 1},
                                                               {"item_id": OTHER_ITEM, "quantity": 1}])
    again, _, _ = service_requests.submit_order(lab, STUDENT, [{"item_id": ITEM, "quantity": 1},
                                                               {"item_id": OTHER_ITEM, "quantity": 1}])
    candidates = dispatch.order_duplicate_candidates(lab, again)
    assert [group["ref"] for group in candidates] == [first[0]["order_id"]]
    ok, message, done = dispatch.cancel_order(lab, again[0]["order_id"], PROFESSOR, dispatch.CANCEL_DUPLICATE,
                                              original_ref=first[0]["order_id"])
    assert ok and len(done) == 2 and "2 de 2" in message
    by_item = {row["item_id"]: row["id"] for row in first}
    for row in again:
        cancel = traceability.cancel_event(lab.get_trace_events(request_id=row["id"]))
        assert traceability.event_details(cancel)["original_request_id"] == by_item[row["item_id"]]

    # Un pedido con una línea entregada: se cancela lo demás y se avisa que lo entregado va por Devoluciones.
    mixed, _, _ = service_requests.submit_order(lab, STUDENT, [{"item_id": ITEM, "quantity": 1},
                                                               {"item_id": OTHER_ITEM, "quantity": 1}])
    delivered = next(row for row in mixed if row["item_id"] == ITEM)
    _advance(lab, delivered["id"], STEPS[traceability.STAGE_DELIVERED])
    ok, message, done = dispatch.cancel_order(lab, mixed[0]["order_id"], ADMIN, dispatch.CANCEL_CLIENT_ERROR)
    assert ok and [row["item_id"] for row in done] == [OTHER_ITEM] and "Devoluciones" in message
    assert _stage(lab, delivered["id"]) == traceability.STAGE_DELIVERED
    assert not dispatch.cancel_order(lab, mixed[0]["order_id"], ADMIN, dispatch.CANCEL_CLIENT_ERROR)[0]


def test_a_whole_order_duplicate_needs_every_product_in_the_original(lab):
    single, _, _ = service_requests.submit_request(lab, STUDENT, "product", ITEM, 1)
    order, _, _ = service_requests.submit_order(lab, STUDENT, [{"item_id": ITEM, "quantity": 1},
                                                               {"item_id": OTHER_ITEM, "quantity": 1}])
    assert dispatch.order_duplicate_candidates(lab, order) == []
    ok, message, done = dispatch.cancel_order(lab, order[0]["order_id"], PROFESSOR, dispatch.CANCEL_DUPLICATE,
                                              original_ref=single["id"])
    assert not ok and "Osciloscopio" in message and not done
    assert {lab.get_service_request(row["id"])["status"] for row in order} == {"pending"}    # no se tocó nada


# --- trazabilidad y lo que ve el cliente ----------------------------------------------------------

def test_timeline_and_client_tracker_say_who_where_and_why(lab):
    original = _order(lab, traceability.STAGE_APPROVED)
    duplicate = _order(lab, traceability.STAGE_CLAIMED)
    assert _cancel(lab, duplicate, reason=dispatch.CANCEL_DUPLICATE, original=original)[0]
    request, events = lab.get_service_request(duplicate), lab.get_trace_events(request_id=duplicate)
    steps = traceability.request_timeline(request, events)
    last = steps[-1]
    assert last["title"] == "Solicitud cancelada" and last["state"] == "blocked"
    assert last["detail"] == (f"Carlos (Supervisor) · Canceló el pedido en picking: pedido duplicado de "
                              f"{traceability.short_id(original)}")
    assert not any(step["state"] == "current" for step in steps)                 # no sigue sumando tiempo
    assert not traceability.is_stuck(request, events, 1, now=datetime.now(timezone.utc) + timedelta(days=3))
    progress = tracker.client_tracker(request, events)
    assert progress["headline"] == "Cancelada"
    assert progress["hint"] == (f"Lo canceló Carlos (Supervisor) en picking: Pedido duplicado de "
                                f"{traceability.short_id(original)}.")


def test_a_cancel_before_anyone_took_it_also_shows_the_dispatch_timeline(lab):
    request_id = _order(lab, traceability.STAGE_REQUESTED)
    assert _cancel(lab, request_id, ADMIN, dispatch.CANCEL_CLIENT_ERROR, "pidió el modelo equivocado")[0]
    steps = traceability.request_timeline(lab.get_service_request(request_id), lab.get_trace_events(request_id=request_id))
    assert [step["title"] for step in steps] == ["Solicitud creada", "Solicitud cancelada"]
    assert "Administrador" in steps[-1]["detail"] and "en revisión: error del cliente (pidió el modelo equivocado)" in \
        steps[-1]["detail"]


def test_legacy_cancellations_are_described_as_before(lab):
    event = {"event_type": traceability.EVENT_ORDER_CANCELLED, "details": '{"reason": "Cancelada"}'}
    assert traceability.describe_event(event) == "Canceló la solicitud · «Cancelada»"
    assert tracker.cancel_hint([event]) == ""


# --- Tiempos, métricas y CSV ------------------------------------------------------------------------

def test_cancelled_orders_leave_averages_but_stay_in_the_times_csv(lab):
    done = _order(lab, traceability.STAGE_DELIVERED, quantity=1)
    cancelled = _order(lab, traceability.STAGE_PICKED, quantity=1)
    for request_id in (done, cancelled):          # el almacenamiento de prueba no pone la hora de la solicitud
        lab.service_requests[request_id]["created_at"] = "2026-11-07T15:00:00+00:00"
    assert _cancel(lab, cancelled)[0]
    rows = {row["request_id"]: row for row in dispatch.dispatch_metrics(lab, ATTENDANT)}
    assert rows[cancelled]["stage"] == traceability.STAGE_CANCELLED and rows[cancelled]["cancelled_at"]
    assert rows[cancelled]["claim_seconds"] == rows[cancelled]["picking_seconds"] == ""     # no suma tiempo
    assert rows[done]["cancelled_at"] == "" and rows[done]["delivery_seconds"] != ""
    summary = dispatch.time_summary(list(rows.values()))
    assert summary["orders"] == 1 and summary["cancelled"] == 1
    assert summary["delivery_seconds"] == rows[done]["delivery_seconds"]
    data = list(csv.DictReader(io.StringIO(dispatch.dispatch_csv(lab, ATTENDANT).decode("utf-8-sig"))))
    assert list(data[0])[-1] == "cancelled_at"
    assert sorted(row["stage"] for row in data) == [traceability.STAGE_CANCELLED, traceability.STAGE_DELIVERED]
    assert any(row["cancelled_at"] for row in data if row["stage"] == traceability.STAGE_CANCELLED)


def test_the_ludica_csv_names_the_event_pedido_cancelado(lab):
    request_id = _order(lab, traceability.STAGE_APPROVED)
    assert _cancel(lab, request_id)[0]
    rows = list(csv.DictReader(io.StringIO(ludica.events_csv(lab, ATTENDANT).decode("utf-8"))))
    kinds = [row["tipo_evento"] for row in rows if row["transaccion_id"] == ludica.pseudonym(request_id)]
    assert kinds[-1] == ludica.KIND_CANCELLED == "pedido_cancelado"
    assert traceability.EVENT_ORDER_CANCELLED not in kinds
    # Un intento rechazado se registra con el mismo nombre y resultado «rechazado».
    lab.transition_request(_order(lab, traceability.STAGE_APPROVED), traceability.EVENT_ORDER_CANCELLED, OPERATOR, {})
    rejected = [row for row in ludica.event_rows(lab) if row["resultado_sistema"] == ludica.RESULT_REJECTED]
    assert [row["tipo_evento"] for row in rejected] == ["pedido_cancelado"]


def test_research_metrics_exclude_cancelled_orders(lab):
    lab.set_setting(event_log.SETTING, "1")
    lab.set_setting(ludica.SETTING_SESSION_ID, "S1")
    kept = _order(lab, traceability.STAGE_DELIVERED, quantity=1)
    cancelled = _order(lab, traceability.STAGE_PICKED, quantity=1)
    assert _cancel(lab, cancelled)[0]
    log = event_log.rows(lab)
    assert any(row["tipo_evento"] == "pedido_cancelado" for row in log)
    assert research_metrics.cancelled_transactions(log) == {ludica.pseudonym(cancelled)}
    summary = research_metrics.summarize(log, by_session=False)
    assert len(summary) == 1 and summary[0]["transacciones"] == 1                   # solo el entregado
    times = event_log.times_from_rows([row for row in log if row["transaccion_id"] == ludica.pseudonym(kept)])
    assert summary[0]["tiempo_picking_promedio_s"] == float(times[ludica.pseudonym(kept)]["picking_seconds"])
    # Sin cancelar, las dos transacciones contarían.
    uncancelled = [row for row in log if row["tipo_evento"] != "pedido_cancelado"]
    assert research_metrics.summarize(uncancelled, by_session=False)[0]["transacciones"] == 2
    data = list(csv.DictReader(io.StringIO(dispatch.dispatch_csv(lab, ATTENDANT).decode("utf-8-sig"))))
    assert list(data[0])[7:] == list(event_log.TIME_COLUMNS) + ["cancelled_at"]


# --- aviso de posible duplicado -----------------------------------------------------------------------

def _two_requests(storage, gap_seconds, item=ITEM, second_item=None, who=STUDENT, hour=15):
    first, _, _ = service_requests.submit_request(storage, who, "product", item, 1)
    second, _, _ = service_requests.submit_request(storage, who, "product", second_item or item, 1)
    base = datetime(2026, 11, 7, hour, 0, tzinfo=timezone.utc)
    storage.service_requests[first["id"]]["created_at"] = base.isoformat()
    storage.service_requests[second["id"]]["created_at"] = (base + timedelta(seconds=gap_seconds)).isoformat()
    return first["id"], second["id"]


def test_the_real_case_two_seconds_apart_is_flagged_on_both_cards(lab):
    first, second = _two_requests(lab, 2)
    dups = dispatch.possible_duplicates(lab)
    assert dups[second] == [{"id": first, "seconds": 2, "earlier": True, "order_id": ""}]
    assert dups[first] == [{"id": second, "seconds": 2, "earlier": False, "order_id": ""}]


def test_outside_the_window_other_product_or_other_client_is_not_flagged(lab):
    _two_requests(lab, 3 * 60, hour=9)                         # fuera de la ventana de 2 min
    _two_requests(lab, 1, second_item=OTHER_ITEM, hour=12)     # otro producto
    assert dispatch.possible_duplicates(lab) == {}
    someone = {"id": "s2", "full_name": "Luis", "institutional_email": "luis@uniminuto.edu.co", "role": "estudiante"}
    lab.users["s2"] = {**someone, "status": "active"}
    luis = service_requests.submit_request(lab, someone, "product", ITEM, 1)[0]
    lab.service_requests[luis["id"]]["created_at"] = "2026-11-07T09:00:01+00:00"    # otro cliente, 1 s después
    assert dispatch.possible_duplicates(lab) == {}


def test_cancelled_or_rejected_requests_stop_the_warning(lab):
    first, second = _two_requests(lab, 2)
    assert _cancel(lab, second, reason=dispatch.CANCEL_DUPLICATE, original=first)[0]
    assert dispatch.possible_duplicates(lab) == {}


def test_the_window_is_an_admin_setting_and_zero_turns_it_off(lab):
    first, second = _two_requests(lab, 5 * 60)
    assert dispatch.duplicate_window_minutes(lab) == 2.0 and not dispatch.possible_duplicates(lab)
    with pytest.raises(PermissionError):
        dispatch.set_duplicate_window(lab, PROFESSOR, 10)
    assert not dispatch.set_duplicate_window(lab, ADMIN, 999)[0]
    assert not dispatch.set_duplicate_window(lab, ADMIN, "mucho")[0]
    assert dispatch.set_duplicate_window(lab, ADMIN, "10")[0]
    assert dispatch.duplicate_window_minutes(lab) == 10.0 and set(dispatch.possible_duplicates(lab)) == {first, second}
    ok, message = dispatch.set_duplicate_window(lab, ADMIN, 0)
    assert ok and "apagado" in message and dispatch.possible_duplicates(lab) == {}
    lab.set_setting(dispatch.SETTING_DUPLICATE_WINDOW, "basura")
    assert dispatch.duplicate_window_minutes(lab) == dispatch.DEFAULT_DUPLICATE_WINDOW


# --- vistas reales -------------------------------------------------------------------------------------

def _view_script():
    import importlib

    import streamlit as st

    from core.storage import LabStorage

    st.session_state.storage = LabStorage()
    st.session_state.user = st.session_state.test_user
    importlib.import_module(f"views.{st.session_state.test_view}").render()


def _open(view, person):
    pytest.importorskip("streamlit.testing.v1")
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(_view_script, default_timeout=60)
    at.session_state["test_user"] = {k: person[k] for k in ("id", "full_name", "role", "institutional_email")}
    at.session_state["test_view"] = view
    at.run()
    assert not at.exception
    return at


@pytest.fixture
def real():
    from core.storage import LabStorage

    storage = LabStorage()
    people = {role: storage.create_user(f"Persona {role}", f"{role}@uniminuto.edu.co", "x", role)
              for role in permissions.ROLES}
    storage.save_item({"name": "Multímetro", "item_type": "standalone", "quantity": 9}, ITEM, is_new=True,
                      actor_email="seed")
    storage.set_setting("picking_route_required", "0", "seed")
    return storage, people


def _cancel_buttons(at):
    return [b for b in at.button if (b.key or "").startswith("cancel_open_") or (b.key or "").startswith("cancel_dup_")
            or "_cancel_" in (b.key or "") or (b.key or "").startswith("order_cancel_")]


def test_only_professor_and_admin_see_cancel_in_despacho_and_solicitudes(real):
    storage, people = real
    rows = {}
    for role in permissions.ROLES:
        if permissions.has_cap(people[role], permissions.CAP_REQUEST):
            rows[role] = service_requests.submit_request(storage, people[role], "product", ITEM, 1)[0]
    for role in permissions.ROLES:
        expected = role in (permissions.ROLE_PROFESSOR, permissions.ROLE_MASTER)
        if permissions.has_cap(people[role], permissions.CAP_VIEW_DISPATCH_QUEUE):
            assert bool(_cancel_buttons(_open("despacho", people[role]))) == expected, ("despacho", role)
        if role in rows:
            assert bool(_cancel_buttons(_open("solicitudes", people[role]))) == expected, ("solicitudes", role)


def test_professor_cancels_from_despacho_with_reason_and_confirmation(real):
    storage, people = real
    row = service_requests.submit_request(storage, people["estudiante"], "product", ITEM, 2)[0]
    assert service_requests.review_request(storage, row["id"], "approved", people["encargado"])[0]
    assert dispatch.claim_request(storage, row["id"], people["encargado"])[0]
    assert dispatch.complete_picking(storage, row["id"], people["encargado"],
                                     readings=[{"code": ITEM, "method": "manual"}] * 2)[0]
    at = _open("despacho", people["profesor"])
    at.button(key=f"cancel_open_deliver_{row['id']}").click().run()
    assert not at.exception
    text = " ".join(str(m.value) for m in at.markdown)
    assert "etapa actual: **Picking completo · por recibir**" in text and "2 unidad(es) vuelven al estante" in text
    confirm = at.button(key=f"cancel_confirm_{row['id']}")
    assert confirm.disabled                                                     # falta el motivo
    at.radio(key=f"cancel_reason_{row['id']}").set_value(dispatch.CANCEL_OTHER).run()
    assert at.button(key=f"cancel_confirm_{row['id']}").disabled               # «Otro» exige texto
    at.text_input(key=f"cancel_text_{row['id']}").input("El curso se aplazó").run()
    at.button(key=f"cancel_confirm_{row['id']}").click().run()
    assert not at.exception
    assert any("Pedido cancelado" in str(s.value) for s in at.success)
    assert storage.get_service_request(row["id"])["status"] == "cancelled"
    assert storage.get_available_quantity(ITEM) == 9
    client = _open("solicitudes", people["estudiante"])
    html = " ".join(str(m.value) for m in client.markdown) + " ".join(str(c.value) for c in client.caption)
    assert "Cancelada" in html and "Lo canceló Persona profesor (Supervisor)" in html and "El curso se aplazó" in html


def test_possible_duplicate_warning_and_shortcut_by_role(real):
    storage, people = real
    first = service_requests.submit_request(storage, people["estudiante"], "product", ITEM, 1)[0]
    second = service_requests.submit_request(storage, people["estudiante"], "product", ITEM, 1)[0]
    attendant = _open("despacho", people["encargado"])
    warnings = " ".join(str(w.value) for w in attendant.warning)
    assert "Posible duplicado" in warnings and traceability.short_id(first["id"]) in warnings
    assert "Posible duplicado" in " ".join(str(m.value) for m in attendant.markdown)          # la pastilla
    assert not [b for b in attendant.button if (b.key or "").startswith("cancel_dup_")]        # sin atajo
    professor = _open("despacho", people["profesor"])
    shortcut = [b for b in professor.button if (b.key or "").startswith("cancel_dup_")]
    assert len(shortcut) == 1 and traceability.short_id(first["id"]) in shortcut[0].label
    shortcut[0].click().run()
    assert professor.radio(key=f"cancel_reason_{second['id']}").value == dispatch.CANCEL_DUPLICATE
    professor.button(key=f"cancel_confirm_{second['id']}").click().run()
    assert not professor.exception
    cancel = traceability.cancel_event(storage.get_trace_events(request_id=second["id"]))
    assert traceability.event_details(cancel)["original_request_id"] == first["id"]
    assert storage.get_service_request(first["id"])["status"] == "pending"


def test_despacho_settings_change_the_duplicate_window(real):
    storage, people = real
    at = _open("despacho", people["maestro"])
    at.number_input(key="duplicate_window_input").set_value(5.0).run()
    at.button(key="duplicate_window_save").click().run()
    assert not at.exception and dispatch.duplicate_window_minutes(storage) == 5.0


def test_trazabilidad_shows_the_cancellation_in_words(real):
    storage, people = real
    row = service_requests.submit_request(storage, people["estudiante"], "product", ITEM, 1)[0]
    assert dispatch.cancel_request(storage, row["id"], people["maestro"], dispatch.CANCEL_NO_STOCK)[0]
    at = _open("trazabilidad", people["profesor"])
    html = " ".join(str(m.value) for m in at.markdown) + " ".join(str(c.value) for c in at.caption)
    assert "Persona maestro (Administrador)" in html
    assert "Canceló el pedido en revisión: sin stock" in html
