# -*- coding: utf-8 -*-
"""views/perfil.py - Perfil del usuario logueado: datos propios y cambio de contrasena."""

import streamlit as st

from core import auth
from core.ui import ROLE_LABELS, badge_html, card, esc, initials, kv_list, page_header, section_title

_ROLE_TONES = {"estudiante": "success", "profesor": "info", "maestro": "warning"}


def render():
    storage = st.session_state.storage
    user = st.session_state.user

    page_header("Mi perfil", icon=":material/person:", subtitle="Tus datos y seguridad de la cuenta")

    col_info, col_security = st.columns([1.15, 1], gap="large")
    with col_info:
        section_title("Datos de la cuenta", icon=":material/badge:")
        with card("perfil_datos"):
            role = user["role"]
            st.markdown(
                f'<div class="lab-profile"><div class="lab-profile__avatar" aria-hidden="true">'
                f'{esc(initials(user["full_name"]))}</div><div style="min-width:0;">'
                f'<div class="lab-profile__name">{esc(user["full_name"])}</div>'
                f'{badge_html(ROLE_LABELS.get(role, role), _ROLE_TONES.get(role, "neutral"), "👤")}'
                f"</div></div>",
                unsafe_allow_html=True,
            )
            kv_list([
                ("ID Estudiante", user.get("student_id") or "N/A"),
                ("Correo institucional", user["institutional_email"]),
                ("Programa / departamento", user.get("program_or_department") or "N/A"),
            ])

    with col_security:
        section_title("Cambiar contraseña", icon=":material/lock:", caption="Usa al menos 8 caracteres.")
        with st.form("change_password_form", clear_on_submit=True):
            current_pw = st.text_input("Contraseña actual", type="password")
            new_pw = st.text_input("Nueva contraseña", type="password")
            new_pw2 = st.text_input("Confirmar nueva contraseña", type="password")
            submitted = st.form_submit_button("Actualizar contraseña", type="primary", use_container_width=True)

            if submitted:
                fresh_user = storage.get_user_by_email(user["institutional_email"])
                if not auth.verify_password(current_pw, fresh_user.get("password_hash", "")):
                    st.error("La contrasena actual no es correcta.")
                elif len(new_pw) < 8:
                    st.error("La nueva contrasena debe tener al menos 8 caracteres.")
                elif new_pw != new_pw2:
                    st.error("Las nuevas contrasenas no coinciden.")
                else:
                    storage.update_user(user["id"], {"password_hash": auth.hash_password(new_pw)})
                    st.success("Contrasena actualizada correctamente.")
