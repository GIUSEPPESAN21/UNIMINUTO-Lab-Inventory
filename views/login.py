# -*- coding: utf-8 -*-
"""views/login.py - Pantalla de inicio de sesion y registro."""

import streamlit as st

from core import auth
from core.ui import centered_logo

LOGO_URL = (
    "https://upload.wikimedia.org/wikipedia/commons/d/db/"
    "Logotipo_de_la_Corporaci%C3%B3n_Universitaria_Minuto_de_Dios.svg"
)


def render():
    storage = st.session_state.storage

    col_a, col_b, col_c = st.columns([1, 2, 1])
    with col_b:
        centered_logo(LOGO_URL, width=110)
        st.markdown(
            '<h1 class="main-header" style="margin-top:0.5rem; margin-bottom:0;">Inventario de Laboratorio</h1>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<p class="page-subtitle">UNIMINUTO · Gestión de inventario y préstamos '
            "para laboratorios de ingeniería</p>",
            unsafe_allow_html=True,
        )
        st.markdown("---")

        tab_login, tab_register = st.tabs(["🔐 Iniciar sesion", "📝 Registrarme"])

        with tab_login:
            with st.form("login_form"):
                email = st.text_input("Correo institucional", placeholder="nombre@uniminuto.edu.co")
                password = st.text_input("Contrasena", type="password")
                submitted = st.form_submit_button("Ingresar", type="primary", use_container_width=True)

                if submitted:
                    user, error = auth.login_user(storage, email, password)
                    if error:
                        st.error(error)
                    else:
                        st.session_state.user = user
                        st.rerun()

        with tab_register:
            st.caption("Solo se aceptan correos institucionales (terminados en **.edu** o **.edu.co**).")
            st.caption("Todos los campos son obligatorios.")
            with st.form("register_form"):
                full_name = st.text_input("Nombre completo *", placeholder="Nombre y apellido")
                student_id = st.text_input("ID Estudiante *", placeholder="Ej: TI2024001")
                email = st.text_input(
                    "Correo institucional *", key="reg_email", placeholder="nombre@tuinstitucion.edu.co"
                )
                program = st.text_input("Programa academico o departamento *")
                password = st.text_input("Contrasena *", type="password", key="reg_pw")
                password2 = st.text_input("Confirmar contrasena *", type="password", key="reg_pw2")
                submitted = st.form_submit_button("Crear cuenta", type="primary", use_container_width=True)

                if submitted:
                    if password != password2:
                        st.error("Las contrasenas no coinciden.")
                    else:
                        user, error = auth.register_user(storage, full_name, email, password, program, student_id)
                        if error:
                            st.error(error)
                        else:
                            st.success(
                                f"¡Cuenta creada como **{user['role']}**! Ya puedes iniciar sesion en la pestana anterior."
                            )
