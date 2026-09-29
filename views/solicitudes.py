# -*- coding: utf-8 -*-
"""Solicitudes de productos y servicios del laboratorio."""

from datetime import datetime, timedelta

import streamlit as st

from core import reservations, service_requests
from core.ui import page_header
from views.location_guide import render_location_guide

_TYPE_LABELS = {
    "Producto del inventario": service_requests.TYPE_PRODUCT,
    "Servicio del laboratorio": service_requests.TYPE_SERVICE,
}
_STATUS_LABELS = {
    "pending": "🟡 Pendiente", "approved": "🟢 Aprobada",
    "rejected": "🔴 Rechazada", "cancelled": "⚪ Cancelada",
}


def _request_title(row: dict) -> str:
    return row.get("item_name") if row.get("request_type") == "product" else row.get("service_name")


def _local(value) -> str:
    return reservations.as_bogota(value).strftime("%d/%m/%Y %I:%M %p") if value else "Sin fecha"


def _request_card(storage, row: dict, user: dict, allow_cancel=True) -> None:
    with st.container(border=True):
        c1, c2 = st.columns([3, 1])
        title = _request_title(row) or "Solicitud"
        quantity = f" · {row.get('quantity')} unidad(es)" if row.get("request_type") == "product" else ""
        c1.markdown(f"**{title}**{quantity} · {_STATUS_LABELS.get(row.get('status'), row.get('status'))}")
        c1.caption(
            f"Solicitante: {row.get('requester_name')} · Requerido: {_local(row.get('needed_at'))}"
        )
        if row.get("description"):
            c1.write(row["description"])
        if row.get("review_notes"):
            c1.caption(f"Respuesta: {row['review_notes']}")
        if row.get("email_notified"):
            c1.caption("✉️ Administradores notificados")
        elif row.get("email_error"):
            c1.caption(f"⚠️ {row['email_error']}")
        if allow_cancel and row.get("status") in ("pending", "approved"):
            if c2.button("Cancelar", key=f"cancel_req_{row['id']}", use_container_width=True):
                ok, message = service_requests.cancel_request(storage, row["id"], user)
                (st.success if ok else st.error)(message)
                if ok:
                    st.rerun()


def render():
    storage = st.session_state.storage
    user = st.session_state.user
    reviewer = user.get("role") in service_requests.REVIEWER_ROLES

    page_header(
        "Solicitudes", icon="📝",
        subtitle="Solicita productos o servicios; el responsable recibe una notificación automática",
    )
    titles = ["➕ Nueva solicitud", "📋 Mis solicitudes"]
    if reviewer:
        titles.append("✅ Gestionar")
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
                except ValueError as exc:
                    st.error(str(exc))

    with tabs[1]:
        mine = storage.get_service_requests(user_id=user["id"])
        if not mine:
            st.info("Todavía no tienes solicitudes.")
        for row in mine:
            _request_card(storage, row, user)

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