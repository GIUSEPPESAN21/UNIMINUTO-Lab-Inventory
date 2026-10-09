# -*- coding: utf-8 -*-
"""views/trazabilidad.py - Ruta verificable hasta un producto y trazabilidad
chequeable de solicitudes, préstamos y servicios.

- Estudiante: indicadores, su ruta de retiro (escaneando cada etiqueta) y la
  línea de tiempo de cada solicitud propia. Nunca ve datos de otros.
- Profesor/maestro: buscador de solicitudes con línea de tiempo completa,
  validación de comprobantes, cadena de custodia de un producto y avance de
  servicios (en curso / entregado)."""

import streamlit as st

from core import location, service_requests, traceability
from core.ui import badge_html, empty_state, esc, page_header, section_title, stat_cards, timeline
from views.location_guide import render_route_record, render_verifiable_route

_STATUS_FILTERS = {
    "Todas": None, "Pendientes": service_requests.STATUS_PENDING, "Aprobadas": service_requests.STATUS_APPROVED,
    "Rechazadas": service_requests.STATUS_REJECTED, "Canceladas": service_requests.STATUS_CANCELLED,
}
_TYPE_FILTERS = {"Todos": None, "Productos": service_requests.TYPE_PRODUCT, "Servicios": service_requests.TYPE_SERVICE}
_FEEDBACK = {"success": st.success, "warning": st.warning, "info": st.info, "danger": st.error}
MAX_RESULTS = 20
MAX_CUSTODY_STEPS = 40


def _page_link(page_key: str, label: str, icon: str) -> None:
    page = (st.session_state.get("pages") or {}).get(page_key)
    if page is not None:
        st.page_link(page, label=label, icon=icon, use_container_width=True)


def _reviewer_names(storage, requests: list) -> dict:
    """Nombre de quien revisó cada solicitud (sin cargar todos los usuarios)."""
    names = {}
    for email in {row.get("reviewed_by") for row in requests if row.get("reviewed_by")}:
        found = storage.get_user_by_email(email)
        if found:
            names[email] = found.get("full_name") or email
    return names


def _loan_for(events: list, loans: dict):
    pickup = traceability.first_event(events, traceability.EVENT_PICKED_UP)
    return loans.get(pickup.get("loan_id")) if pickup else None


def _request_header(row: dict, events: list, loan: dict, show_requester: bool) -> None:
    stage, tone = traceability.request_stage(row, events, loan)
    product = row.get("request_type") == service_requests.TYPE_PRODUCT
    quantity = f" · {row.get('quantity')} u." if product else ""
    meta = traceability.short_id(row.get("id"))
    if show_requester:
        meta += f" · {row.get('requester_name') or 'Solicitante'}"
    if row.get("needed_at"):
        meta += f" · requerido {traceability.fmt_local(row.get('needed_at'))}"
    st.markdown(
        f"<div><b>{esc(traceability.request_title(row))}</b>{esc(quantity)}</div>"
        f"<div>{badge_html(stage, tone)} {badge_html('Producto' if product else 'Servicio')} "
        f"<span class=\"lab-section__caption\">{esc(meta)}</span></div>",
        unsafe_allow_html=True,
    )


def _request_card(row: dict, events: list, loan: dict, names: dict, show_requester: bool) -> None:
    with st.container(border=True):
        _request_header(row, events, loan, show_requester)
        with st.expander("Ver línea de tiempo", expanded=False):
            timeline(traceability.request_timeline(row, events, loan, names))
            route = traceability.first_event(events, traceability.EVENT_ROUTE_VERIFIED)
            if route:
                st.markdown("**Ruta registrada**")
                render_route_record(route)


# ---------------------------------------------------------------------------
# Estudiante
# ---------------------------------------------------------------------------

def _render_student(storage, user: dict) -> None:
    requests = traceability.visible_requests(storage, user)
    events = traceability.visible_events(storage, user)
    index = traceability.events_by_request(events)
    loans = {loan["id"]: loan for loan in storage.get_all_loans() if loan.get("user_id") == user.get("id")}
    open_loans = [loan for loan in loans.values() if loan.get("status") == "out"]
    stat_cards(traceability.student_summary(requests, events, open_loans))

    ready = [
        row for row in requests
        if row.get("request_type") == service_requests.TYPE_PRODUCT
        and row.get("status") == service_requests.STATUS_APPROVED
        and not traceability.first_event(index.get(row["id"]), traceability.EVENT_PICKED_UP)
    ]
    tab_route, tab_list = st.tabs([f"🧭 Ruta de retiro ({len(ready)})", f"📜 Mis solicitudes ({len(requests)})"])

    with tab_route:
        if not ready:
            empty_state(
                "Nada por retirar",
                "Cuando aprueben una solicitud de producto, aquí tendrás la ruta paso a paso para llegar a él.",
                icon="🧭",
            )
            _page_link("solicitudes", "Hacer una solicitud", "📝")
        else:
            options = {row["id"]: row for row in ready}
            ids = list(options)
            choice = ids[0]
            if len(ids) > 1:
                focus = st.session_state.get("trace_focus_request")
                choice = st.selectbox(
                    "Solicitud aprobada", ids, index=ids.index(focus) if focus in ids else 0,
                    format_func=lambda rid: (f"{traceability.request_title(options[rid])} · "
                                             f"{options[rid].get('quantity')} u. · {traceability.short_id(rid)}"),
                    key="trace_route_choice",
                )
            row = options[choice]
            section_title(f"Ruta hacia {traceability.request_title(row)}", icon="🧭",
                          caption="Escanea (o escribe) el código de cada etiqueta en el camino. Al llegar "
                                  "obtienes un comprobante que el profesor puede validar.")
            render_verifiable_route(storage, row, user, key_prefix="trace_route")

    with tab_list:
        if not requests:
            empty_state("Todavía no tienes solicitudes", "Tus solicitudes y su avance aparecerán aquí.", icon="📜")
        names = _reviewer_names(storage, requests)
        for row in requests:
            events_row = index.get(row["id"], [])
            _request_card(row, events_row, _loan_for(events_row, loans), names, show_requester=False)


# ---------------------------------------------------------------------------
# Profesor / maestro
# ---------------------------------------------------------------------------

def _render_search(requests: list, events: list, index: dict, loans: dict, names: dict) -> None:
    query = st.text_input(
        "Buscar", key="trace_query",
        placeholder="Solicitud (#a1b2c3), código o nombre del producto, estudiante o comprobante",
    )
    status_col, type_col = st.columns(2)
    status = _STATUS_FILTERS[status_col.selectbox("Estado", list(_STATUS_FILTERS), key="trace_status")]
    kind = _TYPE_FILTERS[type_col.selectbox("Tipo", list(_TYPE_FILTERS), key="trace_type")]
    results = [
        row for row in traceability.search_requests(requests, query, events)
        if (status is None or row.get("status") == status) and (kind is None or row.get("request_type") == kind)
    ]
    if not results:
        empty_state("Sin resultados",
                    "Prueba con el código del producto, el nombre del estudiante o el número de la solicitud.",
                    icon="🔎")
        return
    shown = results[:MAX_RESULTS]
    st.caption(f"{len(results)} solicitud(es)" + (f"; se muestran {len(shown)}." if len(results) > len(shown) else "."))
    for row in shown:
        events_row = index.get(row["id"], [])
        _request_card(row, events_row, _loan_for(events_row, loans), names, show_requester=True)


def _render_receipt_check(storage, user: dict, names: dict, loans: dict) -> None:
    st.caption("El estudiante muestra el comprobante de 6 caracteres que obtuvo al terminar la ruta. "
               "Valídalo aquí antes de entregar el producto.")
    with st.form("trace_receipt_form"):
        code = st.text_input("Comprobante", placeholder="Ej: K7Q-2MX", max_chars=12)
        if st.form_submit_button("🔏 Validar comprobante", type="primary", use_container_width=True):
            st.session_state.trace_receipt = traceability.verify_receipt(storage, code)

    verification = st.session_state.get("trace_receipt")
    if not verification:
        return
    _FEEDBACK.get(verification.get("tone"), st.info)(verification["message"])
    for warning in verification.get("warnings") or []:
        st.caption(f"⚠️ {warning}")
    if not verification.get("valid"):
        return

    request = verification.get("request")
    if request:
        events_row = storage.get_trace_events(request_id=request["id"])
        _request_card(request, events_row, _loan_for(events_row, loans), names, show_requester=True)
    render_route_record(verification["event"])

    checks = verification.get("checks") or []
    if checks:
        st.caption("Validado por: " + ", ".join(
            f"{check.get('actor_name') or 'Profesor'} ({traceability.fmt_local(check.get('created_at'))})"
            for check in checks
        ))
    if not any(check.get("actor_id") == user.get("id") for check in checks):
        if st.button("✅ Registrar que validé este comprobante", key="trace_receipt_record", use_container_width=True):
            ok, message, _ = traceability.record_receipt_check(storage, verification, user)
            st.session_state.trace_receipt = traceability.verify_receipt(storage, verification["receipt"])
            st.session_state.trace_receipt_notice = (ok, message)
            st.rerun()
    notice = st.session_state.pop("trace_receipt_notice", None)
    if notice:
        (st.success if notice[0] else st.warning)(notice[1])


def _render_custody(storage, requests: list, names: dict) -> None:
    with st.form("trace_custody_form"):
        code = st.text_input("Código del producto", placeholder="Escanea o escribe, p. ej. 2-1-01-01-001")
        if st.form_submit_button("🔗 Ver cadena de custodia", use_container_width=True):
            st.session_state.trace_custody_code = (code or "").strip()
    code = st.session_state.get("trace_custody_code")
    if not code:
        st.caption("Muestra quién ha tenido el producto, cada movimiento de inventario, sus solicitudes, "
                   "rutas verificadas, retiros y validaciones.")
        return
    item = storage.get_item(code)
    if not item:
        st.warning("No hay ningún producto registrado con ese código.")
        return

    open_loans = storage.get_open_loans_for_item(item["id"])
    item_requests = [row for row in requests if row.get("item_id") == item["id"]]
    open_requests = [row for row in item_requests if row.get("status") in (
        service_requests.STATUS_PENDING, service_requests.STATUS_APPROVED)]
    parent = storage.get_item(item.get("parent_id")) if item.get("parent_id") else None
    stat_cards([
        {"label": "Unidades del laboratorio", "value": item.get("quantity", 0), "icon": "📦"},
        {"label": "Disponibles", "value": storage.get_available_quantity(item["id"]), "icon": "✅"},
        {"label": "En préstamo", "value": sum(int(loan.get("quantity") or 0) for loan in open_loans), "icon": "🤝"},
        {"label": "Solicitudes abiertas", "value": len(open_requests), "icon": "📝"},
    ])
    route = location.compact_route(location.build_route_checkpoints(item, parent))
    st.caption(f"{item.get('name')} · {item['id']} · ruta {route}")

    if open_loans:
        section_title("¿Quién lo tiene ahora?", icon="🤝")
        for loan in open_loans:
            due = loan.get("expected_return_at")
            st.caption(
                f"• {loan.get('user_name')} ({loan.get('user_role')}) · {loan.get('quantity')} u. · desde "
                f"{traceability.fmt_local(loan.get('checkout_at'))}"
                + (f" · devolver antes de {traceability.fmt_local(due)}" if due else "")
            )

    section_title("Historial del producto", icon="🔗",
                  caption="Lo más reciente primero: movimientos de inventario, solicitudes y trazabilidad.")
    steps = traceability.custody_timeline(
        storage.get_item_history(item["id"]), item_requests,
        storage.get_trace_events(item_id=item["id"]), names,
    )
    if not steps:
        empty_state("Sin movimientos", "Este producto todavía no tiene historial.", icon="🔗")
        return
    steps = list(reversed(steps))
    timeline(steps[:MAX_CUSTODY_STEPS])
    if len(steps) > MAX_CUSTODY_STEPS:
        st.caption(f"Se muestran los {MAX_CUSTODY_STEPS} registros más recientes de {len(steps)}.")


def _render_services(storage, user: dict, requests: list, index: dict, names: dict) -> None:
    services = [row for row in requests if row.get("request_type") == service_requests.TYPE_SERVICE
                and row.get("status") == service_requests.STATUS_APPROVED]
    active = [row for row in services if traceability.service_stage(index.get(row["id"])) != "delivered"]
    delivered = [row for row in services if row not in active]
    notice = st.session_state.pop("trace_service_notice", None)
    if notice:
        (st.success if notice[0] else st.error)(notice[1])
    if not active:
        empty_state("Sin servicios por atender", "Los servicios aprobados aparecen aquí hasta su entrega.", icon="🛠️")
    for row in active:
        events_row = index.get(row["id"], [])
        stage = traceability.service_stage(events_row)
        with st.container(border=True):
            _request_header(row, events_row, None, show_requester=True)
            if row.get("description"):
                st.caption(row["description"])
            with st.form(f"trace_service_{row['id']}"):
                notes = st.text_input("Nota para el estudiante (opcional)", key=f"trace_service_notes_{row['id']}")
                cols = st.columns(2 if stage == "approved" else 1)
                start = cols[0].form_submit_button(
                    "▶️ Marcar en curso", use_container_width=True) if stage == "approved" else False
                deliver = cols[-1].form_submit_button("📦 Marcar entregado", type="primary", use_container_width=True)
                if start or deliver:
                    target = traceability.EVENT_SERVICE_STARTED if start else traceability.EVENT_SERVICE_DELIVERED
                    ok, message, _ = traceability.mark_service_stage(storage, row["id"], target, user, notes)
                    st.session_state.trace_service_notice = (ok, message)
                    st.rerun()
    if delivered:
        with st.expander(f"Entregados ({len(delivered)})"):
            for row in delivered:
                events_row = index.get(row["id"], [])
                _request_card(row, events_row, None, names, show_requester=True)


def _render_manager(storage, user: dict) -> None:
    requests = traceability.visible_requests(storage, user)
    events = traceability.visible_events(storage, user)
    index = traceability.events_by_request(events)
    loans = {loan["id"]: loan for loan in storage.get_all_loans()}
    names = {row["institutional_email"]: row.get("full_name") or row["institutional_email"]
             for row in storage.get_all_users() if row.get("institutional_email")}
    stat_cards(traceability.manager_summary(requests, events))

    tab_search, tab_receipt, tab_custody, tab_services = st.tabs(
        ["🔎 Seguimiento", "🔏 Validar comprobante", "🔗 Cadena de custodia", "🛠️ Servicios"]
    )
    with tab_search:
        _render_search(requests, events, index, loans, names)
    with tab_receipt:
        _render_receipt_check(storage, user, names, loans)
    with tab_custody:
        _render_custody(storage, requests, names)
    with tab_services:
        _render_services(storage, user, requests, index, names)


def render():
    storage = st.session_state.storage
    user = st.session_state.user
    page_header("Trazabilidad", icon="🧭", subtitle="Ruta verificable y seguimiento de solicitudes")
    if traceability.is_manager(user):
        _render_manager(storage, user)
    else:
        _render_student(storage, user)
