# -*- coding: utf-8 -*-
"""Solicitudes de productos y servicios del laboratorio."""

from datetime import datetime, timedelta

import streamlit as st

from core import reservations, service_requests, traceability
from core.ui import badge_html, esc, page_header, timeline
from views.location_guide import render_location_guide, render_verifiable_route

_TYPE_LABELS = {
    "Producto del inventario": service_requests.TYPE_PRODUCT,
    "Servicio del laboratorio": service_requests.TYPE_SERVICE,
}
# Estado de la solicitud como pastilla: (texto, tono, icono).
_STATUS_BADGES = {
    "pending": ("Pendiente", "warning", "⏳"), "approved": ("Aprobada", "success", "✅"),
    "rejected": ("Rechazada", "danger", "⛔"), "cancelled": ("Cancelada", "neutral", "⚪"),
}


def _request_title(row: dict) -> str:
    return row.get("item_name") if row.get("request_type") == "product" else row.get("service_name")


def _local(value) -> str:
    return reservations.as_bogota(value).strftime("%d/%m/%Y %I:%M %p") if value else "Sin fecha"


def _render_tracking(storage, row: dict, user: dict, events: list, loan: dict) -> None:
    """Seguimiento chequeable de la solicitud y, si ya está aprobada, su ruta."""
    actionable = (
        row.get("request_type") == service_requests.TYPE_PRODUCT
        and row.get("status") == service_requests.STATUS_APPROVED
        and row.get("requester_id") == user.get("id")
        and not traceability.first_event(events, traceability.EVENT_PICKED_UP)
    )
    with st.expander(":material/explore: Ruta y seguimiento" if actionable else ":material/location_on: Seguimiento", expanded=actionable):
        timeline(traceability.request_timeline(row, events, loan))
        if actionable:
            st.markdown("**Ruta verificable hasta el producto**")
            st.caption("Escanea (o escribe) el código de cada etiqueta en el camino; al llegar obtienes "
                       "un comprobante que el profesor puede validar.")
            render_verifiable_route(storage, row, user, key_prefix="req_route")


def _request_card(storage, row: dict, user: dict, allow_cancel=True, events: list = None,
                  loan: dict = None, show_tracking: bool = False) -> None:
    with st.container(border=True):
        c1, c2 = st.columns([3, 1])
        title = _request_title(row) or "Solicitud"
        quantity = f" · {row.get('quantity')} unidad(es)" if row.get("request_type") == "product" else ""
        # HTML escapado (y no markdown): un nombre de producto no puede inyectar enlaces ni imágenes.
        c1.markdown(
            f"<b>{esc(title)}</b>{esc(quantity)} "
            f"{badge_html(*_STATUS_BADGES.get(row.get('status'), (row.get('status'), 'neutral', None)))}",
            unsafe_allow_html=True,
        )
        if show_tracking:
            stage, tone = traceability.request_stage(row, events, loan)
            c1.markdown(
                f"{badge_html(stage, tone)} <span class=\"lab-section__caption\">"
                f"{esc(traceability.short_id(row.get('id')))}</span>",
                unsafe_allow_html=True,
            )
        c1.caption(
            f"Solicitante: {row.get('requester_name')} · Requerido: {_local(row.get('needed_at'))}"
        )
        if row.get("description"):
            c1.write(row["description"])
        if row.get("review_notes"):
            c1.caption(f"Respuesta: {row['review_notes']}")
        if row.get("email_notified"):
            c1.caption(":material/mail: Administradores notificados")
        elif row.get("email_error"):
            c1.caption(f":material/warning: {row['email_error']}")
        # Una solicitud ya retirada sigue su curso por el préstamo: no se ofrece cancelarla.
        picked_up = bool(traceability.first_event(events, traceability.EVENT_PICKED_UP))
        if allow_cancel and row.get("status") in ("pending", "approved") and not picked_up:
            if c2.button("Cancelar", key=f"cancel_req_{row['id']}", use_container_width=True):
                ok, message = service_requests.cancel_request(storage, row["id"], user)
                (st.success if ok else st.error)(message)
                if ok:
                    st.rerun()
        if show_tracking:
            _render_tracking(storage, row, user, events or [], loan)


def render():
    storage = st.session_state.storage
    user = st.session_state.user
    reviewer = user.get("role") in service_requests.REVIEWER_ROLES

    page_header(
        "Solicitudes", icon=":material/edit_note:",
        subtitle="Solicita productos o servicios; el responsable recibe una notificación automática",
    )
    titles = [":material/add: Nueva solicitud", ":material/history: Mis solicitudes"]
    if reviewer:
        titles.append(":material/fact_check: Gestionar")
    tabs = st.tabs(titles)

    with tabs[0]:
        type_label = st.radio("Tipo de solicitud", list(_TYPE_LABELS), horizontal=True, key="request_type")
        request_type = _TYPE_LABELS[type_label]
        selected_item = None
        item_id = ""

        if request_type == service_requests.TYPE_PRODUCT:
            items = [
                item for item in storage.get_all_items()
                if item.get("status") != "retired" and item.get("item_type") != "master"
            ]
            options = {f"{item['name']} ({item['id']})": item for item in items}
            if not options:
                st.warning("No hay productos activos disponibles para solicitar.")
            else:
                choice = st.selectbox("Producto", list(options), key="request_product")
                selected_item = options[choice]
                item_id = selected_item["id"]
                available = storage.get_available_quantity(item_id)
                st.metric("Disponibles ahora", available)
                parent = storage.get_item(selected_item.get("parent_id")) if selected_item.get("parent_id") else None
                render_location_guide(selected_item, parent, key_prefix=f"request_guide_{item_id}")

        tomorrow = datetime.now(reservations.BOGOTA_TZ) + timedelta(days=1)
        with st.form("service_request_form", clear_on_submit=False):
            service_name = ""
            quantity = 1
            if request_type == service_requests.TYPE_PRODUCT:
                max_value = max(1, storage.get_available_quantity(item_id)) if item_id else 1
                quantity = st.number_input("Cantidad", min_value=1, max_value=max_value, value=1, step=1)
            else:
                service_name = st.text_input(
                    "Servicio requerido", placeholder="Ej: Impresión 3D, corte láser o asesoría"
                )
            description = st.text_area(
                "Descripción", placeholder="Explica el uso, curso, materiales o resultado esperado"
            )
            c1, c2 = st.columns(2)
            needed_date = c1.date_input("Fecha requerida", value=tomorrow.date())
            needed_time = c2.time_input("Hora requerida", value=tomorrow.time().replace(tzinfo=None))
            submitted = st.form_submit_button("Enviar solicitud", type="primary", use_container_width=True)

            if submitted:
                needed_at = datetime.combine(
                    needed_date, needed_time, tzinfo=reservations.BOGOTA_TZ
                )
                try:
                    row, notified, message = service_requests.submit_request(
                        storage, user, request_type, item_id, quantity,
                        service_name, description, needed_at,
                    )
                    st.success(f"Solicitud #{row['id']} registrada y pendiente de aprobación.")
                    if notified:
                        st.info(message)
                    else:
                        st.warning(message)
                    if request_type == service_requests.TYPE_PRODUCT:
                        st.caption(":material/explore: Cuando la aprueben, abre **Mis solicitudes** o **Trazabilidad** para "
                                   "recorrer la ruta verificable hasta el producto.")
                    else:
                        st.caption(":material/location_on: Sigue su avance (aprobada → en curso → entregado) en **Mis solicitudes** "
                                   "o en **Trazabilidad**.")
                except ValueError as exc:
                    st.error(str(exc))

    with tabs[1]:
        mine = storage.get_service_requests(user_id=user["id"])
        if not mine:
            st.info("Todavía no tienes solicitudes.")
        # Eventos y préstamos propios: cada tarjeta muestra su línea de tiempo.
        index = traceability.events_by_request(storage.get_trace_events(user_id=user["id"]))
        loans = {loan["id"]: loan for loan in storage.get_all_loans() if loan.get("user_id") == user["id"]}
        for row in mine:
            events = index.get(row["id"], [])
            pickup = traceability.first_event(events, traceability.EVENT_PICKED_UP)
            loan = loans.get(pickup.get("loan_id")) if pickup else None
            _request_card(storage, row, user, events=events, loan=loan, show_tracking=True)

    if reviewer:
        with tabs[2]:
            pending = storage.get_service_requests(status=service_requests.STATUS_PENDING)
            st.metric("Pendientes", len(pending))
            if not pending:
                st.success("No hay solicitudes pendientes.")
            for row in pending:
                with st.expander(
                    f"{_request_title(row)} · {row.get('requester_name')} · {_local(row.get('needed_at'))}",
                    expanded=False,
                ):
                    _request_card(storage, row, user, allow_cancel=False)
                    with st.form(f"review_request_{row['id']}"):
                        notes = st.text_input("Observación para el solicitante")
                        c1, c2 = st.columns(2)
                        approve = c1.form_submit_button("Aprobar", type="primary", use_container_width=True)
                        reject = c2.form_submit_button("Rechazar", use_container_width=True)
                        if approve or reject:
                            decision = (
                                service_requests.STATUS_APPROVED if approve
                                else service_requests.STATUS_REJECTED
                            )
                            ok, message = service_requests.review_request(
                                storage, row["id"], decision, user, notes
                            )
                            (st.success if ok else st.error)(message)
                            if ok:
                                st.rerun()
            st.caption(
                "Aprobar un producto no registra su salida: el retiro se confirma en Escanear para "
                "mantener la cadena de custodia existente."
            )
            st.caption(
                ":material/explore: En **Trazabilidad** validas el comprobante de ruta del estudiante, ves la cadena de "
                "custodia de cada producto y marcas los servicios como en curso o entregados."
            )