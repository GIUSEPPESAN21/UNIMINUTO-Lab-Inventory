# -*- coding: utf-8 -*-
"""Cola operativa de despacho, diseñada para teléfono y lector de códigos."""

from datetime import datetime, timezone

import streamlit as st

from core import dispatch, live_data, permissions, traceability
from core.ui import badge_html, empty_state, esc, guard_cap, page_header, section_title

_DISPATCH_EVERY = live_data.run_every(live_data.dispatch_refresh_seconds())


def _heading(request: dict, events: list) -> None:
    stage, tone = traceability.request_stage(request, events)
    st.markdown(
        f"<b>{esc(request.get('item_name') or request.get('item_id'))}</b> "
        f"{badge_html(stage, tone)}<br>"
        f"<span class='lab-section__caption'>{esc(traceability.short_id(request.get('id')))} · "
        f"{esc(request.get('requester_name'))} · {int(request.get('quantity') or 0)} u.</span>",
        unsafe_allow_html=True,
    )


def _notice(ok: bool, message: str) -> None:
    st.session_state.dispatch_notice = (ok, message)


@st.fragment(run_every=_DISPATCH_EVERY)
def _queue_fragment(storage, user: dict) -> None:
    try:
        rows = dispatch.queue_for(storage, user)
    except PermissionError as exc:
        st.error(str(exc))
        return
    section_title(f"Por tomar ({len(rows)})", icon=":material/inbox:")
    if not rows:
        empty_state("Cola al día", "No hay pedidos autorizados disponibles.", icon=":material/inbox:")
        return
    for request in rows:
        events = storage.get_trace_events(request_id=request["id"])
        with st.container(border=True):
            _heading(request, events)
            if st.button("Tomar pedido", key=f"claim_{request['id']}", type="primary",
                         use_container_width=True):
                ok, message, _ = dispatch.claim_request(storage, request["id"], user)
                _notice(ok, message)
                st.rerun()


@st.fragment(run_every=_DISPATCH_EVERY)
def _mine_fragment(storage, user: dict) -> None:
    rows = dispatch.my_orders(storage, user)
    section_title(f"Mis pedidos ({len(rows)})", icon=":material/person_pin:")
    if not rows:
        empty_state("Sin pedidos asignados", "Toma uno desde la cola cuando estés listo.",
                    icon=":material/person_pin:")
        return
    for request in rows:
        events = storage.get_trace_events(request_id=request["id"])
        stage = traceability.request_stage_key(request, events)
        with st.container(border=True):
            _heading(request, events)
            if stage == traceability.STAGE_CLAIMED:
                c1, c2 = st.columns(2)
                if c1.button("Iniciar picking", key=f"start_pick_{request['id']}",
                             type="primary", use_container_width=True):
                    st.session_state.dispatch_active_request = request["id"]
                    st.rerun()
                if c2.button("Liberar", key=f"release_{request['id']}", use_container_width=True):
                    ok, message, _ = dispatch.release_request(storage, request["id"], user)
                    _notice(ok, message)
                    st.rerun()
            elif stage == traceability.STAGE_PICKED:
                st.success("Picking completo. Esperando recepción del encargado.")


@st.fragment(run_every=_DISPATCH_EVERY)
def _review_fragment(storage, user: dict) -> None:
    rows = dispatch.review_queue(storage, user)
    section_title(f"Por revisar y entregar ({len(rows)})", icon=":material/fact_check:")
    if not rows:
        empty_state("Sin pedidos por revisar", "Los picking completos aparecerán aquí.",
                    icon=":material/fact_check:")
        return
    for request in rows:
        events = storage.get_trace_events(request_id=request["id"])
        stage = traceability.request_stage_key(request, events)
        with st.container(border=True):
            _heading(request, events)
            if stage == traceability.STAGE_PICKED:
                if st.button("Confirmar recepción", key=f"receive_{request['id']}",
                             type="primary", use_container_width=True):
                    ok, message, _ = dispatch.receive_request(storage, request["id"], user)
                    _notice(ok, message)
                    st.rerun()
            elif stage == traceability.STAGE_RECEIVED:
                c1, c2 = st.columns(2)
                if c1.button("Entregar al cliente", key=f"deliver_{request['id']}",
                             type="primary", use_container_width=True):
                    ok, message, _ = dispatch.deliver_request(storage, request["id"], user)
                    _notice(ok, message)
                    st.rerun()
                if c2.button("Corregir / devolver", key=f"review_fix_{request['id']}",
                             use_container_width=True):
                    st.session_state.dispatch_review_focus = request["id"]
                    st.rerun()


def _picking_panel(storage, user: dict) -> None:
    request_id = st.session_state.get("dispatch_active_request")
    if not request_id:
        return
    request = storage.get_service_request(request_id)
    events = storage.get_trace_events(request_id=request_id) if request else []
    if not request or traceability.request_stage_key(request, events) != traceability.STAGE_CLAIMED:
        st.session_state.pop("dispatch_active_request", None)
        return
    needed = int(request.get("quantity") or 0)
    key = f"dispatch_readings_{request_id}"
    readings = st.session_state.setdefault(key, [])
    st.divider()
    section_title(f"Picking · {request.get('item_name')}", icon=":material/barcode_scanner:",
                  caption=f"Lee una unidad a la vez: {len(readings)}/{needed}.")
    st.progress(min(1.0, len(readings) / needed) if needed else 0.0)
    with st.form(f"pick_scan_{request_id}", clear_on_submit=True):
        code = st.text_input("Código leído", placeholder=request.get("item_id"), autofocus=True)
        method = st.radio("Método", ["barcode", "nfc", "manual"], horizontal=True,
                          format_func={"barcode": "Pistola", "nfc": "NFC", "manual": "Teclado"}.get)
        add = st.form_submit_button("Agregar lectura", type="primary", use_container_width=True)
    if add:
        ok, message = dispatch.validate_reading(request, code)
        if not ok:
            dispatch.record_failed_scan(storage, request_id, user, code, message)
            st.error(message)
        elif len(readings) >= needed:
            st.warning("Ya están todas las unidades requeridas.")
        else:
            readings.append({"code": code.strip(), "method": method,
                             "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds")})
            st.rerun()
    c1, c2 = st.columns(2)
    if c1.button("Deshacer última", disabled=not readings, key=f"undo_pick_{request_id}",
                 use_container_width=True):
        readings.pop()
        st.rerun()
    if c2.button("Completar picking", disabled=len(readings) != needed,
                 key=f"complete_pick_{request_id}", type="primary", use_container_width=True):
        ok, message, _ = dispatch.complete_picking(storage, request_id, user, readings)
        _notice(ok, message)
        if ok:
            st.session_state.pop(key, None)
            st.session_state.pop("dispatch_active_request", None)
        st.rerun()
    st.caption("La actualización automática no cubre este formulario ni borra sus lecturas.")


def _review_action_panel(storage, user: dict) -> None:
    request_id = st.session_state.get("dispatch_review_focus")
    if not request_id:
        return
    request = storage.get_service_request(request_id)
    if not request:
        st.session_state.pop("dispatch_review_focus", None)
        return
    st.divider()
    section_title(f"Decisión · {request.get('item_name')}", icon=":material/rule:")
    with st.form(f"review_action_{request_id}"):
        reason = st.text_area("Motivo obligatorio")
        action = st.radio("Acción", ["rework", "shelf"],
                          format_func={"rework": "Volver al picking", "shelf": "Devolver al estante"}.get)
        submit = st.form_submit_button("Aplicar decisión", type="primary", use_container_width=True)
    if submit:
        result = (dispatch.rework_request(storage, request_id, user, reason)
                  if action == "rework" else dispatch.return_to_shelf(storage, request_id, user, reason))
        _notice(result[0], result[1])
        if result[0]:
            st.session_state.pop("dispatch_review_focus", None)
        st.rerun()


def _oversight(storage, user: dict) -> None:
    if not permissions.has_cap(user, permissions.CAP_REASSIGN):
        return
    claimed = []
    for request in storage.get_service_requests(status="approved"):
        events = storage.get_trace_events(request_id=request["id"])
        if traceability.request_stage_key(request, events) == traceability.STAGE_CLAIMED:
            claimed.append((request, events))
    if not claimed:
        return
    with st.expander(f"Reasignar o liberar ({len(claimed)})"):
        choices = {request["id"]: request for request, _ in claimed}
        request_id = st.selectbox("Pedido", list(choices),
                                  format_func=lambda value: f"{choices[value].get('item_name')} · {traceability.short_id(value)}")
        operators = [row for row in storage.get_all_users()
                     if row.get("status") == "active" and row.get("role") == permissions.ROLE_OPERATOR]
        selected = st.selectbox("Nuevo despachador", operators,
                                format_func=lambda row: row.get("full_name"), index=None,
                                placeholder="Selecciona un operario")
        reason = st.text_input("Motivo de reasignación")
        c1, c2 = st.columns(2)
        if c1.button("Reasignar", disabled=selected is None, use_container_width=True):
            ok, message, _ = dispatch.reassign_request(storage, request_id, user, selected, reason)
            _notice(ok, message)
            st.rerun()
        if c2.button("Liberar a la cola", use_container_width=True):
            ok, message, _ = dispatch.release_request(storage, request_id, user, reason)
            _notice(ok, message)
            st.rerun()


def _reports(storage, user: dict) -> None:
    if not permissions.has_cap(user, permissions.CAP_DISPATCH_REPORTS):
        return
    rows = dispatch.dispatch_metrics(storage, user)
    st.dataframe(rows, use_container_width=True, hide_index=True)
    st.download_button("Descargar tiempos CSV", dispatch.dispatch_csv(storage, user),
                       "tiempos-despacho.csv", "text/csv", use_container_width=True)


def render() -> None:
    storage, user = st.session_state.storage, st.session_state.user
    if not (permissions.has_cap(user, permissions.CAP_VIEW_DISPATCH_QUEUE)
            or permissions.has_cap(user, permissions.CAP_RECEIVE_AND_DELIVER)):
        guard_cap(user, permissions.CAP_VIEW_DISPATCH_QUEUE, "Despacho")
        return
    page_header("Despacho", icon=":material/local_shipping:",
                subtitle="Tomar pedido → picking por unidad → recepción → entrega")
    notice = st.session_state.pop("dispatch_notice", None)
    if notice:
        (st.success if notice[0] else st.error)(notice[1])
    titles, renderers = [], []
    if permissions.has_cap(user, permissions.CAP_VIEW_DISPATCH_QUEUE):
        titles += ["Por tomar", "Mis pedidos"]
        renderers += [_queue_fragment, _mine_fragment]
    if permissions.has_cap(user, permissions.CAP_RECEIVE_AND_DELIVER):
        titles.append("Por revisar")
        renderers.append(_review_fragment)
    if permissions.has_cap(user, permissions.CAP_DISPATCH_REPORTS):
        titles.append("Tiempos")
        renderers.append(_reports)
    tabs = st.tabs(titles)
    for tab, renderer in zip(tabs, renderers):
        with tab:
            renderer(storage, user)
    _picking_panel(storage, user)
    _review_action_panel(storage, user)
    _oversight(storage, user)
