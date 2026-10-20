# -*- coding: utf-8 -*-
"""Cola operativa de despacho, diseñada para teléfono y lector de códigos.

El panel de picking (y el de decisión del supervisor) van ARRIBA: en el teléfono
la persona los ve sin recorrer la lista. Las lecturas del picking viven en
:mod:`core.picking` (memoria del proceso), no en ``st.session_state``: un toque
de chip NFC abre una pestaña nueva y esa pestaña suma la lectura al mismo
picking.
"""

import json

import streamlit as st
import streamlit.components.v1 as components

from core import dispatch, live_data, ludica, permissions, picking, traceability
from core.ui import badge_html, empty_state, esc, guard_cap, page_header, section_title
from views import location_guide, photo_panel

_DISPATCH_EVERY = live_data.run_every(live_data.dispatch_refresh_seconds())


def _notice(ok: bool, message: str) -> None:
    st.session_state.dispatch_notice = (ok, message)


def _order_context(storage, request: dict, key: str, photo: bool = True) -> None:
    """Dónde está el producto (ruta y referencia) y cómo se ve."""
    item = storage.get_item(request.get("item_id") or "")
    if not item:
        return
    parent = storage.get_item(item.get("parent_id") or "") if item.get("parent_id") else None
    try:
        route = location_guide.compact_route_for(storage, item, parent)
    except Exception:  # la guía nunca debe tumbar la cola
        route = ""
    reference = str(item.get("location") or (parent or {}).get("location") or "").strip()
    if route:
        st.markdown(f":material/pin_drop: **Ruta** {esc(route)}")
    if reference:
        st.markdown(f":material/location_on: {esc(reference)}")
    if photo:
        photo_panel.render_scan_photo(item, compact=True)
    location_guide.render_location_guide(item, parent, key_prefix=f"dispatch_guide_{key}")


def _focus_reading_input(label: str) -> None:
    """Enfoca el campo del código: la pistola «teclea» donde esté el foco.

    Streamlit no tiene ``autofocus`` en ``text_input`` (1.65), así que se pide el
    foco desde un iframe de altura cero (mismo origen que la app, como el puente
    de cookie de core.session)."""
    script = (
        "<script>const label = %s; const tries = [0, 150, 500];"
        "tries.forEach(ms => setTimeout(() => {"
        "const box = window.parent.document.querySelector('input[aria-label=' + JSON.stringify(label) + ']');"
        "if (box && window.parent.document.activeElement !== box) box.focus();}, ms));</script>"
    ) % json.dumps(label)
    components.html(script, height=0)


def _heading(request: dict, events: list) -> None:
    stage, tone = traceability.request_stage(request, events)
    st.markdown(
        f"<b>{esc(request.get('item_name') or request.get('item_id'))}</b> "
        f"{badge_html(stage, tone)}<br>"
        f"<span class='lab-section__caption'>{esc(traceability.short_id(request.get('id')))} · "
        f"{esc(request.get('requester_name'))} · {int(request.get('quantity') or 0)} u.</span>",
        unsafe_allow_html=True,
    )


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
            _order_context(storage, request, f"queue_{request['id']}")
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
    active = picking.active_request(user["id"])
    for request in rows:
        events = storage.get_trace_events(request_id=request["id"])
        stage = traceability.request_stage_key(request, events)
        with st.container(border=True):
            _heading(request, events)
            if stage == traceability.STAGE_CLAIMED:
                _order_context(storage, request, f"mine_{request['id']}")
                c1, c2 = st.columns(2)
                if c1.button("Seguir picking" if active == request["id"] else "Iniciar picking",
                             key=f"start_pick_{request['id']}", type="primary", use_container_width=True):
                    ok, message = dispatch.start_picking(storage, request["id"], user)
                    if not ok:
                        _notice(False, message)
                    st.rerun()
                if c2.button("Liberar", key=f"release_{request['id']}", use_container_width=True):
                    ok, message, _ = dispatch.release_request(storage, request["id"], user)
                    _notice(ok, message)
                    st.rerun()
            elif stage == traceability.STAGE_PICKED:
                st.success("Picking completo. Esperando recepción del supervisor.")


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
            _order_context(storage, request, f"review_{request['id']}")
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


@st.fragment(run_every=_DISPATCH_EVERY)
def _picking_progress(storage, user: dict, request_id: str, needed: int) -> None:
    """Avance del picking leído del almacén del proceso: se actualiza solo, así
    una lectura NFC hecha en otra pestaña aparece aquí sin tocar nada."""
    readings = dispatch.current_readings(user, request_id)
    st.progress(min(1.0, len(readings) / needed) if needed else 0.0,
                text=f"{len(readings)} de {needed} unidad(es) leída(s)")
    if readings:
        st.caption(" · ".join(
            f"{number}. {dispatch.METHOD_LABELS.get(row.get('method'), row.get('method'))}"
            for number, row in enumerate(readings, 1)
        ))
    c1, c2, c3 = st.columns(3)
    if c1.button("Deshacer última", disabled=not readings, key=f"undo_pick_{request_id}",
                 use_container_width=True):
        dispatch.undo_reading(user, request_id)
        st.rerun()
    if c2.button("Completar picking", disabled=len(readings) != needed,
                 key=f"complete_pick_{request_id}", type="primary", use_container_width=True):
        ok, message, _ = dispatch.complete_picking(storage, request_id, user)
        _notice(ok, message)
        st.rerun()
    if c3.button("Cerrar panel", key=f"close_pick_{request_id}", use_container_width=True,
                 help="Descarta las lecturas de este panel; el pedido sigue tomado."):
        dispatch.abandon_picking(user, request_id)
        st.rerun()


def _picking_panel(storage, user: dict) -> None:
    if not permissions.has_cap(user, permissions.CAP_PICK):
        return
    request_id = picking.active_request(user["id"])
    if not request_id:
        return
    request = storage.get_service_request(request_id)
    events = storage.get_trace_events(request_id=request_id) if request else []
    if (not request or traceability.request_stage_key(request, events) != traceability.STAGE_CLAIMED
            or dispatch.assigned_picker(events) != user["id"]):
        dispatch.abandon_picking(user, request_id)
        return
    needed = int(request.get("quantity") or 0)
    with st.container(border=True, key="dispatch_picking_panel"):
        section_title(f"Picking · {request.get('item_name')}", icon=":material/barcode_scanner:",
                      caption=f"Lee una unidad a la vez ({needed} en total). {dispatch.scenario_label(storage)}.")
        _order_context(storage, request, f"pick_{request_id}")
        _picking_progress(storage, user, request_id, needed)
        scenario = dispatch.SCENARIOS[dispatch.scenario_key(storage)]
        st.caption(f"{scenario['help']} Las lecturas por NFC se suman solas, también desde otra pestaña.")
        with st.form(f"pick_scan_{request_id}", clear_on_submit=True):
            code = st.text_input("Código leído", placeholder=request.get("item_id"))
            add = st.form_submit_button("Agregar lectura", type="primary", use_container_width=True)
        _focus_reading_input("Código leído")
        if add:
            ok, message, _ = dispatch.register_reading(
                storage, request_id, user, code, dispatch.typed_method(storage))
            _notice(ok, message)
            st.rerun()


def _review_action_panel(storage, user: dict) -> None:
    request_id = st.session_state.get("dispatch_review_focus")
    if not request_id:
        return
    request = storage.get_service_request(request_id)
    if not request:
        st.session_state.pop("dispatch_review_focus", None)
        return
    with st.container(border=True, key="dispatch_review_panel"):
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
    claimed = dispatch.claimed_orders(storage, user)
    if not claimed:
        return
    with st.expander(f"Reasignar o liberar ({len(claimed)})"):
        choices = {request["id"]: request for request, _ in claimed}
        request_id = st.selectbox("Pedido", list(choices),
                                  format_func=lambda value: f"{choices[value].get('item_name')} · {traceability.short_id(value)}")
        operators = [row for row in storage.get_all_users()
                     if row.get("status") == "active" and row.get("role") == permissions.ROLE_OPERATOR]
        selected = st.selectbox("Nuevo operario", operators,
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
    st.download_button(
        "Descargar eventos de la lúdica (CSV sin datos personales)", ludica.events_csv(storage, user),
        f"eventos-{ludica.session_id(storage)}.csv", "text/csv", use_container_width=True,
        help="Columnas: sesion_id, timestamp, estacion, tipo_evento, item_codigo, transaccion_id, "
             "usuario_sim, resultado_sistema. Los identificadores van seudonimizados.",
    )


def _settings(storage, user: dict) -> None:
    """Ajustes del despacho (administrador): el escenario fija cómo se registra lo escrito."""
    if not permissions.has_cap(user, permissions.CAP_DISPATCH_SETTINGS):
        return
    current = dispatch.scenario_key(storage)
    section_title("Escenario de la lúdica", icon=":material/tune:",
                  caption="Lo fija el administrador; quien hace el picking no lo cambia.")
    keys = list(dispatch.SCENARIOS)
    choice = st.selectbox(
        "Escenario", keys, index=keys.index(current), key="dispatch_scenario_choice",
        format_func=lambda key: f"{key} · {dispatch.SCENARIOS[key]['label']}",
    )
    st.caption(f"{dispatch.SCENARIOS[choice]['help']} Método registrado para lo escrito: "
               f"**{dispatch.METHOD_LABELS[dispatch.SCENARIOS[choice]['typed_method']]}**; "
               "las lecturas por chip NFC siempre se registran como NFC.")
    if st.button("Guardar escenario", key="dispatch_scenario_save", type="primary",
                 disabled=choice == current):
        ok, message = dispatch.set_scenario(storage, user, choice)
        _notice(ok, message)
        st.rerun()


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
    # Lo que se está haciendo ahora, arriba: en el teléfono no hay que recorrer la lista.
    _picking_panel(storage, user)
    _review_action_panel(storage, user)
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
    if permissions.has_cap(user, permissions.CAP_DISPATCH_SETTINGS):
        titles.append("Ajustes")
        renderers.append(_settings)
    tabs = st.tabs(titles)
    for tab, renderer in zip(tabs, renderers):
        with tab:
            renderer(storage, user)
    _oversight(storage, user)
