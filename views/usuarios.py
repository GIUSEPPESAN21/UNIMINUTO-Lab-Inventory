# -*- coding: utf-8 -*-
"""views/usuarios.py - Gestion de usuarios y lista blanca de profesores.
Acceso exclusivo del rol 'maestro'."""

import streamlit as st

from core.ui import page_header

ROLES = ["estudiante", "profesor", "maestro"]


def render():
    storage = st.session_state.storage
    current_user = st.session_state.user

    page_header("Usuarios", icon="👥", subtitle="Gestión de cuentas, roles y lista blanca de profesores")

    tab_usuarios, tab_whitelist = st.tabs(["👥 Usuarios", "✅ Lista blanca de profesores"])

    with tab_usuarios:
        st.caption(
            "Aqui puedes activar/desactivar cuentas y cambiar roles. "
            "El rol se asigna manualmente; el registro nunca otorga 'profesor' o 'maestro' por si solo."
        )
        users = storage.get_all_users()
        search = st.text_input("Buscar por nombre o correo")
        if search:
            s = search.lower()
            users = [u for u in users if s in (u.get("full_name") or "").lower() or s in (u.get("institutional_email") or "").lower()]

        for u in users:
            with st.container(border=True):
                c1, c2, c3, c4 = st.columns([3, 2, 2, 2])
                c1.markdown(f"**{u.get('full_name')}**")
                c1.caption(f"{u.get('institutional_email')} · ID: {u.get('student_id') or 'N/A'}")
                c2.caption(f"Programa: {u.get('program_or_department') or 'N/A'}")

                new_role = c3.selectbox(
                    "Rol", ROLES, index=ROLES.index(u.get("role", "estudiante")),
                    key=f"role_{u['id']}", label_visibility="collapsed",
                )
                if new_role != u.get("role"):
                    if u["id"] == current_user["id"] and new_role != "maestro":
                        c3.error("No puedes quitarte tu propio rol de maestro.")
                    elif c3.button("Guardar rol", key=f"save_role_{u['id']}"):
                        storage.update_user(u["id"], {"role": new_role})
                        st.success(f"Rol de {u['full_name']} actualizado a {new_role}.")
                        st.rerun()

                is_active = u.get("status") == "active"
                toggle_label = "🚫 Deshabilitar" if is_active else "✅ Habilitar"
                if u["id"] != current_user["id"] and c4.button(toggle_label, key=f"toggle_{u['id']}", use_container_width=True):
                    storage.update_user(u["id"], {"status": "disabled" if is_active else "active"})
                    st.rerun()

    with tab_whitelist:
        st.caption(
            "Los correos en esta lista obtendran automaticamente el rol 'profesor' al registrarse. "
            "Cualquier otro correo institucional se registra como 'estudiante' por defecto."
        )
        with st.form("add_whitelist_form", clear_on_submit=True):
            email = st.text_input("Correo institucional del profesor")
            if st.form_submit_button("➕ Agregar a la lista blanca", type="primary"):
                if email:
                    storage.add_to_whitelist(email)
                    st.success(f"'{email}' agregado a la lista blanca.")
                    st.rerun()

        st.markdown("---")
        emails = storage.get_whitelist()
        if not emails:
            st.info("La lista blanca esta vacia.")
        for email in emails:
            c1, c2 = st.columns([4, 1])
            c1.write(email)
            if c2.button("Quitar", key=f"remove_wl_{email}"):
                storage.remove_from_whitelist(email)
                st.rerun()
