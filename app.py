# -*- coding: utf-8 -*-
"""
Inventario de Laboratorio UNIMINUTO
Gestion de inventario y prestamos (checkout/checkin) para el laboratorio de
ingenieria de UNIMINUTO, con codigos de barras jerarquicos (contenedor
maestro + items hijos) y login por roles (estudiante / profesor / maestro)
via correo institucional.
"""

from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from core.storage import LabStorage
from core import auth, permissions
from core.ui import LOGO_PATH, SYMBOL_PATH, footer, sync_status_banner, user_chip_html

st.set_page_config(
    page_title="Inventario de Laboratorio UNIMINUTO",
    page_icon=str(SYMBOL_PATH) if SYMBOL_PATH.exists() else "🎓",
    layout="wide",
)


@st.cache_data
def load_css():
    # Ruta relativa a este archivo (no al directorio de trabajo) y UTF-8 explicito:
    # la hoja de estilos tiene tildes y simbolos.
    try:
        return (Path(__file__).resolve().parent / "style.css").read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


css = load_css()
if css:
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


@st.cache_resource
def get_storage():
    return LabStorage()


try:
    storage = get_storage()
except Exception as e:
    st.error(f"**Error critico de inicializacion de la base de datos:** {e}")
    st.stop()

st.session_state.storage = storage
auth.ensure_master_seed(storage)

if "user" not in st.session_state:
    st.session_state.user = None

# Chip NFC: un toque abre la app con ?nfc=<codigo>. Se guarda ANTES del login
# para registrarlo en cuanto la persona inicie sesion (ver views/nfc_tap.py).
from views import nfc_tap
nfc_tap.capture_tap()

if not st.session_state.user:
    from views import login
    nfc_tap.render_login_hint()
    login.render()
    st.stop()

# La sesion se revalida contra la base en CADA recarga: un cambio de rol o una
# cuenta deshabilitada surten efecto de inmediato, y la sesion expira sola.
session_user, session_problem = auth.validate_session(
    storage, st.session_state.user, st.session_state.get("login_at")
)
if session_user is None:
    st.session_state.user = None
    st.session_state.login_at = None
    st.session_state.session_notice = session_problem
    st.rerun()
st.session_state.user = session_user
if st.session_state.get("login_at") is None:
    st.session_state.login_at = datetime.now(timezone.utc)
user = session_user

from views import inicio, escanear, inventario, prestamos, reservas, solicitudes, trazabilidad, usuarios, reportes, acerca_de, perfil
from views import nfc as nfc_page

pages = {
    "inicio": st.Page(inicio.render, title="Inicio", icon=":material/home:", default=True, url_path="inicio"),
    "escanear": st.Page(escanear.render, title="Escanear", icon=":material/barcode_scanner:", url_path="escanear"),
    "solicitudes": st.Page(solicitudes.render, title="Solicitudes", icon=":material/edit_note:", url_path="solicitudes"),
    "trazabilidad": st.Page(trazabilidad.render, title="Trazabilidad", icon=":material/explore:", url_path="trazabilidad"),
    "reservas": st.Page(reservas.render, title="Reservas", icon=":material/calendar_month:", url_path="reservas"),
    "prestamos": st.Page(prestamos.render, title="Préstamos", icon=":material/assignment:", url_path="prestamos"),
}

if user["role"] in permissions.MANAGER_ROLES:
    pages["inventario"] = st.Page(inventario.render, title="Inventario", icon=":material/inventory_2:", url_path="inventario")
    pages["reportes"] = st.Page(reportes.render, title="Reportes", icon=":material/bar_chart:", url_path="reportes")
    pages["nfc"] = st.Page(nfc_page.render, title="Chips NFC", icon=":material/nfc:", url_path="nfc")

if user["role"] in permissions.ADMIN_ROLES:
    pages["usuarios"] = st.Page(usuarios.render, title="Usuarios", icon=":material/group:", url_path="usuarios")

pages["perfil"] = st.Page(perfil.render, title="Mi perfil", icon=":material/person:", url_path="perfil")
pages["acerca_de"] = st.Page(acerca_de.render, title="Acerca de", icon=":material/info:", url_path="acerca-de")

st.session_state.pages = pages

# Navegacion agrupada por secciones para que el sidebar sea facil de leer.
nav_sections = {"Principal": [pages["inicio"], pages["escanear"], pages["solicitudes"], pages["trazabilidad"], pages["reservas"], pages["prestamos"]]}
if user["role"] in permissions.MANAGER_ROLES:
    nav_sections["Gestión"] = [pages["inventario"], pages["reportes"], pages["nfc"]]
if user["role"] in permissions.ADMIN_ROLES:
    nav_sections["Administración"] = [pages["usuarios"]]
nav_sections["Mi cuenta"] = [pages["perfil"], pages["acerca_de"]]

# Marca institucional: el logotipo completo, centrado, encabeza la barra
# lateral; con la barra cerrada (celular) la cabecera muestra solo el simbolo,
# que sigue siendo legible a ese tamaño. Se sirven desde assets/, sin Internet.
if LOGO_PATH.exists():
    st.logo(str(LOGO_PATH), size="large",
            icon_image=str(SYMBOL_PATH) if SYMBOL_PATH.exists() else str(LOGO_PATH))

with st.sidebar:
    st.markdown(user_chip_html(user), unsafe_allow_html=True)
    if st.button("Cerrar sesión", icon=":material/logout:", use_container_width=True):
        st.session_state.user = None
        st.session_state.login_at = None
        st.rerun()

sync_status_banner(storage, user)

nav = st.navigation(nav_sections)
# Toque NFC pendiente: se registra una sola vez y lleva a su pagina.
nfc_tap.handle_pending(storage, user, pages, nav)
nfc_tap.render_tap_banner()
nav.run()
footer()
