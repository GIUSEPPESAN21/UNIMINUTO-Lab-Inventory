# -*- coding: utf-8 -*-
"""views/trazabilidad.py - Ruta verificable hasta un producto y trazabilidad de
solicitudes y prestamos (en construccion)."""

from core.ui import empty_state, page_header


def render():
    page_header("Trazabilidad", icon="🧭", subtitle="Ruta verificable y seguimiento de solicitudes")
    empty_state("En construcción", "Aquí verás la ruta y el seguimiento de cada solicitud.", icon="🧭")
