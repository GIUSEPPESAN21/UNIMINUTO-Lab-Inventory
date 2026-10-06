# -*- coding: utf-8 -*-
"""views/inicio.py - Dashboard principal."""

import streamlit as st

from core import loans as loans_core
from core.labels import ITEM_TYPE_NAMES
from core.ui import page_header


def _quick_guide():
    with st.expander("💡 Guía rápida: ¿cómo funciona este sistema?", expanded=False):
        st.markdown(f"""
        1. **{ITEM_TYPE_NAMES['master']}**: la caja o kit físico que agrupa varios productos.
        2. **{ITEM_TYPE_NAMES['child']}**: una subdivisión dentro de un Contenedor Principal
           para una característica concreta (ej. "Resistencias 220 Ω").
        3. **{ITEM_TYPE_NAMES['standalone']}**: un producto con su propio código, sin contenedor.
        4. Ve a **🛰️ Escanear**, escribe o escanea el código con tu lector USB, y desde ahí puedes
           **dar salida** (llevarte el producto prestado) o **reingresarlo** cuando lo devuelvas.
        5. En **📝 Solicitudes** pide un producto o servicio; los responsables reciben correo si SMTP está configurado.
        6. En **🗓️ Reservas** solicita una actividad o el laboratorio completo y consulta su aprobación.
        7. En **📋 Préstamos** puedes ver en todo momento qué tienes prestado (o, si eres profesor
           o del perfil maestro, quién tiene qué en todo el laboratorio).
        """)


def render():
    user = st.session_state.user
    storage = st.session_state.storage

    page_header(
        f"Bienvenido, {user['full_name'].split(' ')[0]}",
        subtitle=f"{user['role'].capitalize()} · {user.get('program_or_department') or 'UNIMINUTO'}",
    )
    _quick_guide()
    st.markdown("---")

    try:
        items = storage.get_all_items()
        my_open_loans = storage.get_open_loans_for_user(user["id"])
        overdue = loans_core.get_overdue_loans(storage)
    except Exception as e:
        st.error(f"No se pudieron cargar las estadisticas: {e}")
        items, my_open_loans, overdue = [], [], []

    if user["role"] == "estudiante":
        c1, c2, c3 = st.columns(3)
        c1.metric("📦 Items en catalogo", len(items))
        c2.metric("📋 Mis prestamos activos", len(my_open_loans))
        c3.metric("⚠️ Vencidos (mios)", len([l for l in my_open_loans if loans_core.is_overdue(l)]))
    else:
        all_open = storage.get_all_loans(status="out")
        users = storage.get_all_users()
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("📦 Items en catalogo", len(items))
        c2.metric("📋 Prestamos activos", len(all_open))
        c3.metric("⚠️ Vencidos", len(overdue))
        c4.metric("👥 Usuarios registrados", len(users))

    st.markdown("---")

    if not items:
        st.info(
            "🧪 El inventario todavía está vacío."
            + (
                " Ve a **📦 Inventario → Nuevo item** (o **Importar CSV masivo**) "
                "para registrar los primeros productos del laboratorio."
                if user["role"] in ("profesor", "maestro")
                else " Pídele a un profesor o al administrador del laboratorio que registre los primeros productos."
            )
        )
        return

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Accesos rapidos")
        pages = st.session_state.get("pages", {})
        if st.button("🛰️ Escanear codigo", use_container_width=True) and "escanear" in pages:
            st.switch_page(pages["escanear"])
        if user["role"] != "estudiante" and "inventario" in pages:
            if st.button("📦 Ir a Inventario", use_container_width=True):
                st.switch_page(pages["inventario"])
        if "solicitudes" in pages and st.button("📝 Crear solicitud", use_container_width=True):
            st.switch_page(pages["solicitudes"])
        if "reservas" in pages and st.button("🗓️ Reservar laboratorio", use_container_width=True):
            st.switch_page(pages["reservas"])
        if "prestamos" in pages and st.button("📋 Ver prestamos", use_container_width=True):
            st.switch_page(pages["prestamos"])

    with col2:
        st.subheader("⚠️ Alertas")
        availability = storage.get_availability_map()
        low_stock = [
            i for i in items
            if i.get("item_type") != "master"
            and i.get("min_stock_alert") is not None
            and availability.get(i["id"], 0) <= i.get("min_stock_alert", 0)
        ]
        if not low_stock and not overdue:
            st.success("Sin alertas por el momento.")
        else:
            with st.container(height=220):
                for i in low_stock:
                    st.warning(f"**{i.get('name')}**: disponibilidad baja ({availability.get(i['id'], 0)} u.)")
                if user["role"] != "estudiante":
                    for l in overdue:
                        st.error(f"**Vencido:** '{l.get('item_name')}' con {l.get('user_name')}")
