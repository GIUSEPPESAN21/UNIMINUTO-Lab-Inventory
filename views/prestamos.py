# -*- coding: utf-8 -*-
"""views/prestamos.py - Panel de prestamos: 'Mis prestamos' para estudiantes,
'Quien tiene que' completo para profesor/maestro."""

import streamlit as st

from core import loans as loans_core
from core.ui import card, card_header, empty_state, page_header, section_title, stat_cards


def _when(value, fmt: str = "%d/%m/%Y %H:%M") -> str:
    return value.strftime(fmt) if value else "N/A"


def _loan_row(storage, loan: dict, user: dict, key_prefix: str):
    overdue = loans_core.is_overdue(loan)
    with card(f"loan_{key_prefix}_{loan['id']}", tone="danger" if overdue else "info"):
        status = (
            {"text": "Vencido", "tone": "danger", "icon": "⏰"} if overdue
            else {"text": "Activo", "tone": "info", "icon": "📤"}
        )
        card_header(
            f"{loan['item_name']} ×{loan['quantity']}",
            subtitle=f"Prestado a: {loan['user_name']} ({loan['user_role']})",
            icon="📦",
            badges=[status],
        )
        c1, c3 = st.columns([3, 1], vertical_alignment="center")
        details = [f"Salida: {_when(loan.get('checkout_at'))}"]
        expected = loan.get("expected_return_at")
        if expected:
            details.append(f"Devolver antes de: {_when(expected, '%d/%m/%Y')}")
        c1.caption(" · ".join(details))
        if loan.get("notes"):
            c1.caption(f"Notas: {loan['notes']}")
        can_checkin = user["role"] != "estudiante" or loan["user_id"] == user["id"]
        if can_checkin and c3.button(
            "↩️ Reingresar", key=f"{key_prefix}_{loan['id']}", use_container_width=True,
            type="primary" if overdue else "secondary",
        ):
            ok, msg = loans_core.checkin(storage, loan["id"], user)
            if ok:
                st.success(msg)
                st.rerun()
            else:
                st.error(msg)


def render():
    storage = st.session_state.storage
    user = st.session_state.user

    page_header("Préstamos", icon="📋", subtitle="Salidas y reingresos del laboratorio")

    if user["role"] == "estudiante":
        loans = storage.get_open_loans_for_user(user["id"])
        overdue_count = len([l for l in loans if loans_core.is_overdue(l)])
        stat_cards([
            {"label": "Mis préstamos activos", "value": len(loans), "icon": "📋", "tone": "info"},
            {"label": "Vencidos", "value": overdue_count, "icon": "⏰",
             "tone": "danger" if overdue_count else "success"},
        ])
        section_title("Mis préstamos", icon="📋")
        if not loans:
            empty_state("No tienes préstamos activos.", "Cuando registres una salida desde Escanear aparecerá aquí.",
                        icon="📭")
        for loan in loans:
            _loan_row(storage, loan, user, "mine")
        return

    tab_activos, tab_historial = st.tabs(["📋 Préstamos activos", "🗂️ Historial completo"])

    with tab_activos:
        loans = storage.get_all_loans(status="out")
        overdue_count = len([l for l in loans if loans_core.is_overdue(l)])
        stat_cards([
            {"label": "Préstamos activos", "value": len(loans), "icon": "📋", "tone": "info"},
            {"label": "Vencidos", "value": overdue_count, "icon": "⏰",
             "tone": "danger" if overdue_count else "success"},
        ])
        if not loans:
            empty_state("No hay préstamos activos.", "Todo el material está en el laboratorio.", icon="✅")
        for loan in loans:
            _loan_row(storage, loan, user, "active")

    with tab_historial:
        all_loans = storage.get_all_loans()
        returned = [l for l in all_loans if l.get("status") == "returned"]
        if not returned:
            empty_state("Aún no hay préstamos devueltos en el historial.", icon="🗂️")
        for loan in returned:
            with card(f"loan_hist_{loan['id']}"):
                card_header(
                    f"{loan['item_name']} ×{loan['quantity']}",
                    subtitle=f"{loan['user_name']} ({loan['user_role']})",
                    icon="📦",
                    badges=[{"text": "Devuelto", "tone": "success", "icon": "✅"}],
                )
                st.caption(
                    f"Salida: {_when(loan.get('checkout_at'))} · "
                    f"Reingreso: {_when(loan.get('return_at'))}"
                )
