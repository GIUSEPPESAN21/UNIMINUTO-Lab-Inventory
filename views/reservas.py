# -*- coding: utf-8 -*-
"""Reservas de actividades o del laboratorio completo."""

from datetime import datetime, timedelta

import streamlit as st

from core import reservations
from core.ui import page_header

_SCOPE_LABELS = {
    "Actividad específica": reservations.SCOPE_ACTIVITY,
    "Laboratorio completo": reservations.SCOPE_FULL_LAB,
}
_STATUS_LABELS = {
    "pending": "🟡 Pendiente", "approved": "🟢 Aprobada",
    "rejected": "🔴 Rechazada", "cancelled": "⚪ Cancelada",
}


def _local(value) -> str:
    if not value:
        return "Sin fecha"
    return reservations.as_bogota(value).strftime("%d/%m/%Y %I:%M %p")


def _reservation_card(storage, row: dict, user: dict, allow_cancel=True) -> None:
    with st.container(border=True):
        c1, c2 = st.columns([3, 1])
        c1.markdown(f"**{row.get('activity')}** · {_STATUS_LABELS.get(row.get('status'), row.get('status'))}")
        c1.caption(
            f"{_local(row.get('start_at'))} → {_local(row.get('end_at'))} · "
            f"{row.get('attendees')} asistente(s) · {row.get('requester_name')}"
        )
        c1.write(row.get("purpose") or "Sin propósito")
        if row.get("review_notes"):
            c1.caption(f"Respuesta: {row['review_notes']}")
        if allow_cancel and row.get("status") in ("pending", "approved"):
            if c2.button("Cancelar", key=f"cancel_res_{row['id']}", use_container_width=True):
                ok, message = reservations.cancel_reservation(storage, row["id"], user)
                (st.success if ok else st.error)(message)
                if ok:
                    st.rerun()


def render():
    storage = st.session_state.storage
    user = st.session_state.user
    reviewer = user.get("role") in reservations.REVIEWER_ROLES

    page_header(
        "Reservas", icon="🗓️",
        subtitle="Solicita una actividad o el laboratorio completo y evita cruces de horario",
    )
    titles = ["➕ Nueva reserva", "📅 Mis reservas"]
    if reviewer:
        titles.append("✅ Gestionar")
    tabs = st.tabs(titles)

    with tabs[0]:
        scope_label = st.radio("Alcance", list(_SCOPE_LABELS), horizontal=True, key="reservation_scope")
        scope_type = _SCOPE_LABELS[scope_label]
        now = datetime.now(reservations.BOGOTA_TZ)
        default_start = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        default_end = default_start + timedelta(hours=2)

        with st.form("reservation_form", clear_on_submit=False):
            activity = ""
            if scope_type == reservations.SCOPE_ACTIVITY:
                activity = st.text_input("Actividad", placeholder="Ej: Práctica de robótica")
            purpose = st.text_area("Propósito", placeholder="Curso, grupo y objetivo de la actividad")
            attendees = st.number_input("Número de asistentes", min_value=1, max_value=500, value=10, step=1)
            c1, c2 = st.columns(2)
            start_date = c1.date_input("Fecha de inicio", value=default_start.date())
            start_time = c2.time_input("Hora de inicio", value=default_start.time())
            c3, c4 = st.columns(2)
            end_date = c3.date_input("Fecha de finalización", value=default_end.date())
            end_time = c4.time_input("Hora de finalización", value=default_end.time())
            submitted = st.form_submit_button("Enviar solicitud de reserva", type="primary", use_container_width=True)

            if submitted:
                start_at = datetime.combine(start_date, start_time, tzinfo=reservations.BOGOTA_TZ)
                end_at = datetime.combine(end_date, end_time, tzinfo=reservations.BOGOTA_TZ)
                try:
                    row, notified, message = reservations.submit_reservation(
                        storage, user, scope_type, activity, purpose, attendees, start_at, end_at
                    )
                    st.success(f"Reserva #{row['id']} registrada y pendiente de aprobación.")
                    if notified:
                        st.info(message)
                    else:
                        st.warning(message)
                except ValueError as exc:
                    st.error(str(exc))

    with tabs[1]:
        mine = storage.get_reservations(user_id=user["id"])
        if not mine:
            st.info("Todavía no tienes reservas.")
        for row in mine:
            _reservation_card(storage, row, user)

    if reviewer:
        with tabs[2]:
            pending = storage.get_reservations(status=reservations.STATUS_PENDING)
            approved = storage.get_reservations(status=reservations.STATUS_APPROVED)
            st.metric("Pendientes", len(pending))
            if not pending:
                st.success("No hay reservas pendientes.")
            for row in pending:
                with st.expander(
                    f"{row.get('activity')} · {_local(row.get('start_at'))} · {row.get('requester_name')}",
                    expanded=False,
                ):
                    st.write(row.get("purpose"))
                    st.caption(
                        f"Alcance: {row.get('scope_type')} · {row.get('attendees')} asistentes · "
                        f"Fin: {_local(row.get('end_at'))}"
                    )
                    with st.form(f"review_res_{row['id']}"):
                        notes = st.text_input("Observación para el solicitante")
                        c1, c2 = st.columns(2)
                        approve = c1.form_submit_button("Aprobar", type="primary", use_container_width=True)
                        reject = c2.form_submit_button("Rechazar", use_container_width=True)
                        if approve or reject:
                            decision = reservations.STATUS_APPROVED if approve else reservations.STATUS_REJECTED
                            ok, message = reservations.review_reservation(
                                storage, row["id"], decision, user, notes
                            )
                            (st.success if ok else st.error)(message)
                            if ok:
                                st.rerun()

            st.markdown("### Próximas reservas aprobadas")
            if not approved:
                st.caption("No hay reservas aprobadas.")
            for row in approved:
                _reservation_card(storage, row, user, allow_cancel=True)