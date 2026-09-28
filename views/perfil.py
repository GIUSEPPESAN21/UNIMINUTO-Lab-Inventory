# -*- coding: utf-8 -*-
"""views/perfil.py - Perfil del usuario logueado: datos propios y cambio de contrasena."""

import streamlit as st

from core import auth
from core.ui import page_header

ROLE_LABELS = {"estudiante": "Estudiante", "profesor": "Profesor", "maestro": "Perfil maestro"}


def render():
    storage = st.session_state.storage
    user = st.session_state.user

    page_header("Mi perfil", icon="👤", subtitle="Tus datos y seguridad de la cuenta")
    c1, c2 = st.columns(2)
    c1.metric("Nombre", user["full_name"])
    c2.metric("Rol", ROLE_LABELS.get(user["role"], user["role"]))
    st.caption(f"ID Estudiante: {user.get('student_id') or 'N/A'}")
    st.caption(f"Correo institucional: {user['institutional_email']}")
    st.caption(f"Programa / departamento: {user.get('program_or_department') or 'N/A'}")

    st.markdown("---")
    st.subheader("🔒 Cambiar contrasena")
    with st.form("change_password_form", clear_on_submit=True):
        current_pw = st.text_input("Contrasena actual", type="password")
        new_pw = st.text_input("Nueva contrasena", type="password")
        new_pw2 = st.text_input("Confirmar nueva contrasena", type="password")
        submitted = st.form_submit_button("Actualizar contrasena", type="primary", use_container_width=True)

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
