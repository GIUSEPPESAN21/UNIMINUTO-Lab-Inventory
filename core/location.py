# -*- coding: utf-8 -*-
"""Guías de ubicación móviles derivadas de los códigos GLIOPS.

Este módulo no dibuja un plano: los documentos no incluyen coordenadas ni
puntos de entrada. Produce pasos deterministas y comprobables; para códigos
libres usa la ubicación textual registrada en el ítem o su contenedor padre.
"""

from core import barcode


def _add(steps: list, text: str) -> None:
    text = (text or "").strip()
    if text and text not in steps:
        steps.append(text)


def build_location_guide(item: dict, parent: dict = None) -> dict:
    """Devuelve título, resumen y pasos para llegar a un ítem.

    Nunca lanza por un código heredado: si no puede interpretarlo, degrada a
    `location` y finalmente a una instrucción de consulta al responsable.
    """
    item = item or {}
    parent = parent or {}
    code = str(item.get("id") or "").strip()
    name = str(item.get("name") or "Producto").strip()
    location_text = str(item.get("location") or parent.get("location") or "").strip()
    steps = []
    parsed = None

    navigable_formats = (barcode.FORMAT_STANDARD, barcode.FORMAT_MESA, barcode.FORMAT_LEGO)
    try:
        candidate = barcode.parse_code(code)
        parsed = candidate if candidate.get("format") in navigable_formats else None
    except ValueError:
        parsed = None

    # Un ítem puede tener código libre y heredar la ruta estructurada de su contenedor.
    if not parsed:
        parent_code = str(parent.get("id") or "").strip()
        if parent_code:
            try:
                candidate = barcode.parse_code(parent_code)
                parsed = candidate if candidate.get("format") in navigable_formats else None
            except ValueError:
                parsed = None

    if parsed and parsed.get("format") == barcode.FORMAT_STANDARD:
        _add(steps, "Dirígete al área de estanterías del laboratorio.")
        _add(steps, f"Ubica la Estantería {parsed['estanteria']}.")
        _add(steps, f"Ve al Piso {parsed['piso']} de esa estantería.")
        _add(steps, f"Busca el Contenedor {parsed['contenedor']:02d}.")
        if parsed.get("caja", 0) > 0:
            _add(steps, f"Dentro del contenedor, abre la Caja {parsed['caja']:02d}.")
        if parsed.get("item", 0) > 0:
            _add(steps, f"Localiza el Ítem {parsed['item']:03d} y confirma su etiqueta.")
    elif parsed and parsed.get("format") == barcode.FORMAT_MESA:
        _add(steps, f"Dirígete a la Mesa de trabajo {parsed['mesa']}.")
        _add(steps, f"Busca el Equipo {parsed['equipo']} y confirma su etiqueta.")
    elif parsed and parsed.get("format") == barcode.FORMAT_LEGO:
        _add(steps, "Dirígete a la Estantería 3.")
        _add(steps, "Ubica la zona de modelos Lego en exhibición.")
        _add(steps, f"Busca el Modelo {parsed['modelo']} y confirma su etiqueta.")

    if location_text:
        _add(steps, f"Referencia física registrada: {location_text}.")

    if not steps:
        _add(steps, "Consulta al responsable del laboratorio: este producto aún no tiene una ruta estructurada.")

    _add(steps, f"Verifica que el nombre sea “{name}” y el código sea “{code or 'sin código'}”.")
    return {
        "title": f"Cómo llegar a {name}",
        "code": code,
        "location": location_text,
        "structured": bool(parsed),
        "steps": steps,
    }