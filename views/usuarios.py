# -*- coding: utf-8 -*-
"""Gestión de usuarios y lista blanca, exclusiva del rol maestro."""

import streamlit as st

from core import auth, permissions
from core.ui import (
    ROLE_LABELS, badge_html, card, card_header_html, empty_state, guard_role, initials, page_header,
    section_title, stat_cards,
)

ROLES = ["estudiante", "profesor", "maestro"]
_ROLE_TONES = {"estudiante": "success", "profesor": "info", "maestro": "warning"}


def _user_card(storage, user: dict, current_user: dict) -> None:
    is_active = user.get("status") == "active"
    with card(f"user_{user['id']}", tone=None if is_active else "danger"):
        role = user.get("role", "estudiante")
        badges = [{"text": ROLE_LABELS.get(role, role), "tone": _ROLE_TONES.get(role, "neutral")}]
        badges.append({"text": "Activo", "tone": "success", "icon": "●"} if is_active
                      else {"text": "Deshabilitado", "tone": "danger", "icon": "●"})
        c1, c2, c3, c4 = st.columns([3, 2, 2, 2], vertical_alignment="center")
        c1.markdown(
            card_header_html(
                user.get("full_name"),
                subtitle=f"{user.get('institutional_email')} · ID: {user.get('student_id') or 'N/A'}",
                icon=initials(user.get("full_name")),
                badges=badges,
            ),
            unsafe_allow_html=True,
        )
        c2.caption(f"Programa: {user.get('program_or_department') or 'N/A'}")

        new_role = c3.selectbox(
            "Rol", ROLES, index=ROLES.index(user.get("role", "estudiante")),
            key=f"role_{user['id']}", label_visibility="collapsed",
        )
        if new_role != user.get("role"):
            if user["id"] == current_user["id"] and new_role != "maestro":
                c3.error("No puedes quitarte tu propio rol de maestro.")
            elif c3.button("Guardar rol", key=f"save_role_{user['id']}"):
                storage.update_user(user["id"], {"role": new_role})
                st.success(f"Rol de {user['full_name']} actualizado a {new_role}.")
                st.rerun()

        toggle_label = ":material/block: Deshabilitar" if is_active else ":material/check_circle: Habilitar"
        if user["id"] == current_user["id"]:
            # Nadie se deshabilita a si mismo: en su lugar, una pastilla que
            # conserva la simetria de la fila.
            c4.markdown(f'<div style="text-align:center">{badge_html("Tu cuenta", "info", "👤")}</div>',
                        unsafe_allow_html=True)
        elif c4.button(toggle_label, key=f"toggle_{user['id']}", use_container_width=True):
            storage.update_user(user["id"], {"status": "disabled" if is_active else "active"})
            st.rerun()

        with st.expander(":material/edit: Corregir datos del usuario", expanded=False):
            with st.form(f"edit_user_{user['id']}"):
                full_name = st.text_input("Nombre completo", value=user.get("full_name") or "")
                student_id = st.text_input("ID de estudiante", value=user.get("student_id") or "")
                email = st.text_input(
                    "Correo institucional", value=user.get("institutional_email") or ""
                )
                program = st.text_input(
                    "Programa o departamento", value=user.get("program_or_department") or ""
                )
                submitted = st.form_submit_button(
                    "Guardar correcciones", type="primary", use_container_width=True
                )
                if submitted:
                    changes, error = auth.validate_profile_update(
                        storage, user["id"], full_name, email, program, student_id
                    )
                    if error:
                        st.error(error)
                    else:
                        try:
                            updated = storage.update_user(user["id"], changes)
                            if user["id"] == current_user["id"] and updated:
                                st.session_state.user = auth.public_user({**current_user, **updated})
                            st.success("Datos del usuario actualizados.")
                            st.rerun()
                        except ValueError as exc:
                            st.error(str(exc))


def render():
    storage = st.session_state.storage
    current_user = st.session_state.user

    if not guard_role(current_user, permissions.ADMIN_ROLES, "la gestión de usuarios"):
        return

    page_header(
        "Usuarios", icon=":material/group:",
        subtitle="Corrección de datos, roles, estados y lista blanca de profesores",
    )
    tab_usuarios, tab_whitelist = st.tabs([":material/group: Usuarios", ":material/check_circle: Lista blanca de profesores"])

    with tab_usuarios:
        st.caption(
            "Puedes corregir nombre, ID, correo y programa, además de activar cuentas o cambiar roles. "
            "El registro solo otorga profesor si el correo ya está en la lista blanca."
        )
        users = storage.get_all_users()
        stat_cards([
            {"label": "Usuarios", "value": len(users), "icon": "👥", "tone": "info"},
            {"label": "Profesores", "value": len([u for u in users if u.get("role") == "profesor"]),
             "icon": "👨‍🏫", "tone": "neutral"},
            {"label": "Deshabilitados", "value": len([u for u in users if u.get("status") != "active"]),
             "icon": "🚫", "tone": "neutral"},
        ])
        search = st.text_input(":material/search: Buscar por nombre, correo o ID")
        if search:
            value = search.lower()
            users = [
                user for user in users
                if value in (user.get("full_name") or "").lower()
                or value in (user.get("institutional_email") or "").lower()
                or value in (user.get("student_id") or "").lower()
            ]
        section_title(f"{len(users)} usuario(s)", icon=":material/contacts:")
        for user in users:
            _user_card(storage, user, current_user)

    with tab_whitelist:
        st.caption(
            "Los correos en esta lista obtendrán automáticamente el rol profesor al registrarse. "
            "Cualquier otro correo institucional se registra como estudiante."
        )
        with st.form("add_whitelist_form", clear_on_submit=True):
            email = st.text_input("Correo institucional del profesor")
            if st.form_submit_button(":material/add: Agregar a la lista blanca", type="primary"):
                if not auth.is_institutional_email(email):
                    st.error("Ingresa un correo institucional válido (.edu o .edu.co).")
                else:
                    storage.add_to_whitelist(email)
                    st.success(f"'{email}' agregado a la lista blanca.")
                    st.rerun()

        emails = storage.get_whitelist()
        section_title("Correos autorizados", icon=":material/check_circle:", caption=f"{len(emails)} correo(s) en la lista")
        if not emails:
            empty_state("La lista blanca está vacía.", icon=":material/inbox:")
        for email in emails:
            with card(f"wl_{email}"):
                c1, c2 = st.columns([4, 1], vertical_alignment="center")
                c1.markdown(card_header_html(email, icon=":material/mail:"), unsafe_allow_html=True)
                if c2.button("Quitar", key=f"remove_wl_{email}", use_container_width=True):
                    storage.remove_from_whitelist(email)
                    st.rerun()