# -*- coding: utf-8 -*-
"""views/login.py - Pantalla de inicio de sesion y registro."""

from datetime import datetime, timezone

import streamlit as st

from core import auth
from core.ui import centered_logo

LOGO_URL = (
    "https://upload.wikimedia.org/wikipedia/commons/d/db/"
    "Logotipo_de_la_Corporaci%C3%B3n_Universitaria_Minuto_de_Dios.svg"
)

PENDING_KEY = "pending_registration"


def _show_created_account(user: dict) -> None:
    st.success(f"¡Cuenta creada como **{user['role']}**! Ya puedes iniciar sesion en la pestana anterior.")


def _start_professor_verification(storage, prepared: dict) -> None:
    """El correo esta en la lista blanca de profesores: antes de darle ese rol hay
    que comprobar que el solicitante es dueño del correo (codigo por email)."""
    code, challenge = auth.new_verification_challenge(prepared["email"])
    sent, _message = auth.send_verification_email(prepared["email"], code)
    if sent:
        st.session_state[PENDING_KEY] = {"prepared": prepared, "challenge": challenge}
        st.rerun()
        return

    # Sin correo configurado no hay forma de verificar: nunca se otorga el rol
    # sin verificar; la cuenta nace estudiante y el maestro puede ascenderla.
    user, error = auth.create_account(storage, prepared, email_verified=False)
    if error:
        st.error(error)
        return
    _show_created_account(user)
    st.info(
        "Tu correo esta en la lista de profesores, pero no pudimos enviarte el codigo de "
        "verificacion. Tu cuenta se creo como **estudiante**: pide al perfil maestro que "
        "active tu rol de profesor."
    )


def _render_verification_step(storage) -> None:
    pending = st.session_state[PENDING_KEY]
    prepared = pending["prepared"]
    st.info(
        f"Enviamos un codigo de 6 digitos a **{prepared['email']}**. Escribelo para verificar "
        "tu correo y activar el rol de profesor. Vence en 15 minutos."
    )
    with st.form("verify_registration_form"):
        code = st.text_input("Codigo de verificacion", max_chars=6, placeholder="123456")
        c1, c2, c3 = st.columns(3)
        verify = c1.form_submit_button("Verificar y crear cuenta", type="primary", use_container_width=True)
        resend = c2.form_submit_button("Reenviar codigo", use_container_width=True)
        cancel = c3.form_submit_button("Cancelar", use_container_width=True)

    if cancel:
        st.session_state.pop(PENDING_KEY, None)
        st.rerun()
    elif resend:
        new_code, challenge = auth.new_verification_challenge(prepared["email"])
        sent, message = auth.send_verification_email(prepared["email"], new_code)
        if sent:
            pending["challenge"] = challenge
            st.success("Enviamos un codigo nuevo.")
        else:
            st.error(message)
    elif verify:
        ok, message, challenge = auth.check_verification_code(pending["challenge"], code)
        if ok:
            user, error = auth.create_account(storage, prepared, email_verified=True)
            st.session_state.pop(PENDING_KEY, None)
            if error:
                st.error(error)
            else:
                _show_created_account(user)
        elif challenge is None:
            st.session_state.pop(PENDING_KEY, None)
            st.error(message)
        else:
            pending["challenge"] = challenge
            st.error(message)


def _render_registration_form(storage) -> None:
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
                return
            prepared, error = auth.validate_registration(
                storage, full_name, email, password, program, student_id
            )
            if error:
                st.error(error)
            elif auth.needs_professor_verification(storage, prepared["email"]):
                _start_professor_verification(storage, prepared)
            else:
                user, error = auth.create_account(storage, prepared)
                if error:
                    st.error(error)
                else:
                    _show_created_account(user)


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

        notice = st.session_state.pop("session_notice", None)
        if notice:
            st.warning(notice)

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
                        st.session_state.login_at = datetime.now(timezone.utc)
                        st.rerun()

        with tab_register:
            if st.session_state.get(PENDING_KEY):
                _render_verification_step(storage)
            else:
                _render_registration_form(storage)
