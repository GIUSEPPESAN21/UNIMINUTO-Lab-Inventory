# -*- coding: utf-8 -*-
"""views/acerca_de.py - Informacion del proyecto y como usarlo."""

import streamlit as st

from core.labels import ITEM_TYPE_NAMES
from core.ui import feature_cards, hero, section_title


def render():
    hero(
        "Inventario de Laboratorio UNIMINUTO",
        subtitle="Trazabilidad de equipos y materiales del laboratorio de ingeniería",
        eyebrow="Acerca del proyecto",
        icon="🏢",
    )

    with st.container(border=True, key="lab_card_acerca_intro"):
        st.markdown(
            "Este sistema fue creado para el **laboratorio de ingeniería de UNIMINUTO**, donde "
            "conviven muchos tipos de productos identificados con una serie propia de códigos "
            "de barras. Su objetivo es saber en todo momento **qué equipos y materiales existen, "
            "dónde están y quién los tiene prestados**, sin depender de planillas sueltas."
        )

    section_title("¿Cómo funciona?", icon="🧩", caption="Tres tipos de elementos organizan todo el inventario.")
    feature_cards([
        {"icon": "🗄️", "title": ITEM_TYPE_NAMES["master"],
         "text": "La caja, kit o gabinete físico que agrupa productos relacionados."},
        {"icon": "🧩", "title": ITEM_TYPE_NAMES["child"],
         "text": "Una subdivisión dentro de un Contenedor Principal para una característica "
                 "concreta (ej. \"Resistencias 220 Ω\", \"Tornillos M4\")."},
        {"icon": "🔹", "title": ITEM_TYPE_NAMES["standalone"],
         "text": "Un producto con su propio código, sin contenedor."},
    ])

    section_title("Roles institucionales", icon="👥")
    feature_cards([
        {"icon": "🎓", "title": "Estudiante",
         "text": "Escanea, solicita salida y reingresa lo que él mismo tomó prestado."},
        {"icon": "👨‍🏫", "title": "Profesor",
         "text": "Administra el inventario, registra préstamos de cualquiera y ve reportes."},
        {"icon": "🔐", "title": "Perfil maestro",
         "text": "Gestiona usuarios, roles y tiene visibilidad total del laboratorio."},
    ])

    st.info(
        "Para dudas sobre el uso del sistema o solicitudes de soporte, contacta al "
        "administrador del laboratorio (perfil maestro) desde tu programa académico.",
        icon="💬",
    )
