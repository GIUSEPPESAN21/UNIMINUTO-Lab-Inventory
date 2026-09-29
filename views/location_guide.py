# -*- coding: utf-8 -*-
"""Componente móvil para recorrer paso a paso una ubicación del laboratorio."""

import streamlit as st

from core.location import build_location_guide


def render_location_guide(item: dict, parent: dict = None, key_prefix: str = "location") -> None:
    guide = build_location_guide(item, parent)
    steps = guide["steps"]
    with st.expander(f"🧭 {guide['title']}", expanded=False):
        st.caption("Marca tu avance desde el celular. La ruta usa el código y la ubicación registrada.")
        current = st.radio(
            "Paso actual",
            options=list(range(len(steps))),
            format_func=lambda index: f"Paso {index + 1}",
            key=f"{key_prefix}_step",
            horizontal=len(steps) <= 4,
        )
        st.progress((current + 1) / len(steps))
        st.info(f"**Paso {current + 1}:** {steps[current]}")
        st.markdown("**Ruta completa**")
        for index, step in enumerate(steps, start=1):
            marker = "✅" if index - 1 < current else "➡️" if index - 1 == current else "⬜"
            st.write(f"{marker} **{index}.** {step}")