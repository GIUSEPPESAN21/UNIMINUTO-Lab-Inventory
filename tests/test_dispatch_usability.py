# -*- coding: utf-8 -*-
"""v1.22 · despacho profesional y sencillo: trabajo pendiente por rol, entrega en un clic, picking continuo,
aprobación automática configurable y la pestaña «Por aprobar» en Despacho."""

from datetime import datetime, timedelta, timezone

import pytest

from core import dispatch, loans, returns, service_requests, traceability

STUDENT = {"id": "s1", "full_name": "Ana", "institutional_email": "ana@uniminuto.edu.co", "role": "estudiante"}
OPERATOR = {"id": "o1", "full_name": "Olga", "institutional_email": "olga@uniminuto.edu.co", "role": "operario"}
SUPERVISOR = {"id": "p1", "full_name": "Pablo", "institutional_email": "pablo@uniminuto.edu.co", "role": "encargado"}
ADMIN = {"id": "m1", "full_name": "Maria", "institutional_email": "maria@uniminuto.edu.co", "role": "maestro"}


@pytest.fixture
def lab(storage):
    storage.add_item("LAB-A", name="Pinza", quantity=9)
    storage.add_item("LAB-B", name="Alicate", quantity=9)
    storage.add_item("LAB-C", name="Tijera", quantity=9)
    for person in (STUDENT, OPERATOR, SUPERVISOR, ADMIN):
        storage.users[person["id"]] = {**person, "status": "active"}
    return storage


def _order(storage, *codes):
    rows, _, _ = service_requests.submit_order(storage, STUDENT, [{"item_id": c, "quantity": 1} for c in codes])
    return rows


def _approve(storage, rows):
    for row in rows:
        assert service_requests.review_request(storage, row["id"], "approved", SUPERVISOR)[0]


def _pick(storage, row, actor=OPERATOR):
    assert dispatch.complete_picking(storage, row["id"], actor, readings=[{"code": row["item_id"], "method": "manual"}]
                                     * int(row["quantity"]))[0]


def _counts(storage, user):
    return {row["key"]: row["count"] for row in dispatch.work_summary(storage, user)}


# --- trabajo pendiente por rol ---------------------------------------------------------------

def test_each_role_sees_only_the_work_it_can_do(lab):
    _order(lab, "LAB-A", "LAB-B")                                   # 1 pedido de 2 líneas: cuenta como 1
    _order(lab, "LAB-C")
    assert _counts(lab, SUPERVISOR)[dispatch.WORK_APPROVE] == 2
    assert dispatch.WORK_APPROVE not in _counts(lab, OPERATOR)         # el operario no aprueba
    assert dispatch.WORK_DELIVER not in _counts(lab, OPERATOR)         # ni entrega
    assert dispatch.work_summary(lab, STUDENT) == []                   # el cliente no tiene bandeja de trabajo
    assert [row["key"] for row in dispatch.work_summary(lab, SUPERVISOR)] == [
        dispatch.WORK_APPROVE, dispatch.WORK_PREPARE, dispatch.WORK_MINE, dispatch.WORK_DELIVER, dispatch.WORK_RETURNS]


def test_counts_follow_each_order_through_the_stages(lab):
    rows = _order(lab, "LAB-A", "LAB-B")
    _approve(lab, rows)
    assert _counts(lab, OPERATOR)[dispatch.WORK_PREPARE] == 1
    ok, _, taken = dispatch.claim_order(lab, rows[0]["order_id"], OPERATOR)
    assert ok and _counts(lab, OPERATOR) == {dispatch.WORK_PREPARE: 0, dispatch.WORK_MINE: 1, dispatch.WORK_RETURNS: 0}
    for row in rows:
        _pick(lab, row)
    assert _counts(lab, OPERATOR)[dispatch.WORK_MINE] == 0
    assert _counts(lab, SUPERVISOR)[dispatch.WORK_DELIVER] == 1
    assert dispatch.pending_total(lab, SUPERVISOR) == 1


def test_returns_count_only_what_each_role_handles(lab):
    ok, _, loan = loans.checkout(lab, "LAB-A", 1, STUDENT)
    returns.request_return(lab, loan["id"], STUDENT)
    assert _counts(lab, SUPERVISOR)[dispatch.WORK_RETURNS] == 1       # verifica
    assert _counts(lab, OPERATOR)[dispatch.WORK_RETURNS] == 0         # aún no hay nada que guardar
    returns.verify_return(lab, loan["id"], SUPERVISOR, "ok", 1)
    assert _counts(lab, OPERATOR)[dispatch.WORK_RETURNS] == 1


def test_pending_total_never_breaks_the_menu(lab, monkeypatch):
    monkeypatch.setattr(dispatch, "work_summary", lambda *a: (_ for _ in ()).throw(RuntimeError("caída")))
    assert dispatch.pending_total(lab, SUPERVISOR) == 0


# --- entregar en un clic --------------------------------------------------------------------

def test_hand_over_records_review_and_delivery_in_one_step(lab):
    row = _order(lab, "LAB-A")[0]
    _approve(lab, [row])
    dispatch.claim_request(lab, row["id"], OPERATOR)
    _pick(lab, row)
    with pytest.raises(PermissionError):
        dispatch.hand_over(lab, row["id"], OPERATOR)
    ok, message, _ = dispatch.hand_over(lab, row["id"], SUPERVISOR)
    assert ok and "Entregado al cliente" in message
    kinds = [e["event_type"] for e in lab.get_trace_events(request_id=row["id"])]
    assert kinds[-2:] == [traceability.EVENT_ORDER_RECEIVED, traceability.EVENT_ORDER_DELIVERED]
    assert not dispatch.hand_over(lab, row["id"], SUPERVISOR)[0]       # ya entregado


def test_hand_over_refuses_orders_not_ready(lab):
    row = _order(lab, "LAB-A")[0]
    _approve(lab, [row])
    ok, message, _ = dispatch.hand_over(lab, row["id"], SUPERVISOR)
    assert not ok and "no está listo" in message


def test_hand_over_order_delivers_every_ready_line(lab):
    rows = _order(lab, "LAB-A", "LAB-B", "LAB-C")
    _approve(lab, rows)
    dispatch.claim_order(lab, rows[0]["order_id"], OPERATOR)
    _pick(lab, rows[0])
    _pick(lab, rows[1])                                               # la tercera sigue en preparación
    ok, message, done = dispatch.hand_over_order(lab, rows[0]["order_id"], SUPERVISOR)
    assert ok and len(done) == 2 and "2 de 2" in message
    stage = traceability.request_stage_key(lab.get_service_request(rows[2]["id"]),
                                           lab.get_trace_events(request_id=rows[2]["id"]))
    assert stage == traceability.STAGE_CLAIMED


# --- picking continuo ------------------------------------------------------------------------

def test_next_to_pick_prefers_the_same_order_then_the_oldest(lab):
    first = _order(lab, "LAB-C")
    order = _order(lab, "LAB-A", "LAB-B")
    _approve(lab, first + order)
    dispatch.claim_request(lab, first[0]["id"], OPERATOR)
    dispatch.claim_order(lab, order[0]["order_id"], OPERATOR)
    _pick(lab, order[0])
    assert dispatch.next_to_pick(lab, OPERATOR, order[0])["id"] == order[1]["id"]     # mismo pedido primero
    _pick(lab, order[1])
    assert dispatch.next_to_pick(lab, OPERATOR, order[1])["id"] == first[0]["id"]      # luego lo que queda
    _pick(lab, first[0])
    assert dispatch.next_to_pick(lab, OPERATOR, first[0]) is None
    assert dispatch.next_to_pick(lab, SUPERVISOR) is None                               # lo de otro no cuenta


# --- aprobación automática configurable --------------------------------------------------------

def test_auto_approval_is_an_admin_setting(lab):
    assert not service_requests.auto_approve_products(lab)
    with pytest.raises(PermissionError):
        service_requests.set_auto_approve(lab, SUPERVISOR, True)
    assert service_requests.set_auto_approve(lab, ADMIN, True)[0]
    rows = _order(lab, "LAB-A", "LAB-B")
    assert {lab.get_service_request(r["id"])["status"] for r in rows} == {"approved"}
    assert dispatch.count_orders(dispatch.queue_for(lab, OPERATOR)) == 1
    service, _, _ = service_requests.submit_request(lab, STUDENT, "service", service_name="Corte", description="x")
    assert lab.get_service_request(service["id"])["status"] == "pending"            # los servicios no
    assert service_requests.set_auto_approve(lab, ADMIN, False)[0]
    assert lab.get_service_request(_order(lab, "LAB-C")[0]["id"])["status"] == "pending"


def test_the_setting_wins_over_the_secret(lab, monkeypatch):
    monkeypatch.setattr(service_requests, "safe_secret", lambda key, default=None: "true")
    assert service_requests.auto_approve_products(lab) and service_requests.auto_approve_products()
    lab.set_setting(service_requests.SETTING_AUTO_APPROVE, "0")
    assert not service_requests.auto_approve_products(lab)


# --- antigüedad y urgencia ------------------------------------------------------------------

def test_waiting_text_and_urgency():
    now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    assert dispatch.waiting_text(now - timedelta(seconds=20), now) == "recién"
    assert dispatch.waiting_text(now - timedelta(minutes=25), now) == "hace 25 min"
    assert dispatch.waiting_text(now - timedelta(hours=5), now) == "hace 5 h"
    assert dispatch.waiting_text(now - timedelta(days=3), now) == "hace 3 días"
    assert dispatch.waiting_text(None, now) == ""
    assert dispatch.is_urgent({"needed_at": now + timedelta(hours=3)}, now)
    assert dispatch.is_urgent({"needed_at": now - timedelta(hours=1)}, now)
    assert not dispatch.is_urgent({"needed_at": now + timedelta(days=3)}, now)
    assert not dispatch.is_urgent({}, now)


# --- vistas reales ------------------------------------------------------------------------------

def _view_script():
    import importlib

    import streamlit as st

    from core.storage import LabStorage

    st.session_state.storage = LabStorage()
    st.session_state.user = st.session_state.test_user
    for key, value in (st.session_state.get("test_state") or {}).items():
        st.session_state.setdefault(key, value)
    importlib.import_module(f"views.{st.session_state.test_view}").render()


def _open(view, person, **state):
    pytest.importorskip("streamlit.testing.v1")
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(_view_script, default_timeout=60)
    at.session_state["test_user"] = {k: person[k] for k in ("id", "full_name", "role", "institutional_email")}
    at.session_state["test_view"] = view
    at.session_state["test_state"] = state
    at.run()
    assert not at.exception
    return at


@pytest.fixture
def real():
    from core.storage import LabStorage

    storage = LabStorage()
    people = {role: storage.create_user(f"Persona {role}", f"{role}@uniminuto.edu.co", "x", role)
              for role in ("estudiante", "operario", "encargado", "maestro")}
    for code, name in (("LAB-A", "Pinza"), ("LAB-B", "Alicate")):
        storage.save_item({"name": name, "item_type": "standalone", "quantity": 9}, code, is_new=True,
                          actor_email="seed")
    return storage, people


def test_despacho_board_and_approve_tab_with_rejection_reason(real):
    storage, people = real
    rows, _, _ = service_requests.submit_order(storage, people["estudiante"],
                                               [{"item_id": "LAB-A", "quantity": 2}], description="Para el taller")
    at = _open("despacho", people["encargado"])
    assert [t.label for t in at.tabs][:2] == ["Por aprobar", "Por preparar"]
    text = " ".join(str(m.value) for m in at.markdown)
    captions = " ".join(str(c.value) for c in at.caption)
    assert "Por aprobar" in text and "Pinza × 2" in text and "hay 9 disponible(s)" in captions
    assert "Para el taller" in " ".join(str(i.value) for i in at.info)
    at.text_input(key=f"reject_reason_{rows[0]['id']}").input("Sin stock para esa fecha")
    at.button(key=f"reject_{rows[0]['id']}").click().run()
    assert not at.exception
    row = storage.get_service_request(rows[0]["id"])
    assert row["status"] == "rejected" and row["review_notes"] == "Sin stock para esa fecha"


def test_one_click_delivery_from_the_deliver_tab(real):
    storage, people = real
    row = service_requests.submit_order(storage, people["estudiante"], [{"item_id": "LAB-A", "quantity": 1}])[0][0]
    service_requests.review_request(storage, row["id"], "approved", people["encargado"])
    dispatch.claim_request(storage, row["id"], people["operario"])
    dispatch.complete_picking(storage, row["id"], people["operario"], readings=[{"code": "LAB-A", "method": "manual"}])
    at = _open("despacho", people["encargado"])
    at.button(key=f"handover_{row['id']}").click().run()
    assert not at.exception
    stage = traceability.request_stage_key(storage.get_service_request(row["id"]),
                                           storage.get_trace_events(request_id=row["id"]))
    assert stage == traceability.STAGE_DELIVERED
    assert any("Entregado al cliente" in str(s.value) for s in at.success)


def test_continuous_picking_opens_the_next_product_of_the_order(real):
    storage, people = real
    rows, _, _ = service_requests.submit_order(storage, people["estudiante"],
                                               [{"item_id": "LAB-A", "quantity": 1}, {"item_id": "LAB-B", "quantity": 1}])
    service_requests.review_order(storage, rows[0]["order_id"], "approved", people["encargado"])
    at = _open("despacho", people["operario"])
    at.button(key=f"claim_{rows[0]['order_id']}").click().run()
    from core import picking

    first = picking.active_request(people["operario"]["id"])
    assert first in {r["id"] for r in rows}
    at.text_input[0].input([r for r in rows if r["id"] == first][0]["item_id"])
    [b for b in at.button if b.label == "Registrar lectura"][0].click().run()
    [b for b in at.button if b.label == "Confirmar unidades y completar picking"][0].click().run()
    assert not at.exception
    second = picking.active_request(people["operario"]["id"])
    assert second and second != first                                  # se abrió el siguiente producto solo
    assert any("Sigue:" in str(s.value) for s in at.success)


def test_inicio_shows_the_pending_work_and_the_setting_toggle(real):
    storage, people = real
    service_requests.submit_order(storage, people["estudiante"], [{"item_id": "LAB-A", "quantity": 1}])
    home = _open("inicio", people["encargado"])
    html = " ".join(str(m.value) for m in home.markdown)
    assert "Por aprobar" in html and "Por entregar" in html and "1 pedido(s) esperan algo de ti." in html
    assert html.index("Por aprobar") < html.index("Por preparar") < html.index("Por entregar")
    focused = _open("despacho", people["encargado"], despacho_focus="Por entregar")
    assert focused.session_state["tabs_despacho"] == "Por entregar"
    admin = _open("despacho", people["maestro"])
    admin.toggle(key="auto_approve_toggle").set_value(True).run()
    admin.button(key="auto_approve_save").click().run()
    assert not admin.exception and service_requests.auto_approve_products(storage)
