# -*- coding: utf-8 -*-
"""core/reading.py - Lectura robusta de códigos con pistola, NFC o teclado (M5).

Opción ``lectura_robusta`` (Despacho → Ajustes, apagada por omisión). Activada:

- ``normalize_code`` limpia lo que manda la pistola ANTES de validar: espacios y
  saltos de línea (también los de en medio), caracteres de control (STX/ETX,
  Tab, Enter), el prefijo de simbología AIM (``]C1``, ``]E0``…), los asteriscos de
  inicio y fin de Code 39 y las minúsculas.
- ``is_double_read``: la misma lectura repetida dentro de la ventana
  (``ventana_doble_lectura``, 1,5 s por omisión) se ignora; quien llama deja
  constancia en el registro.
- ``route_mismatch`` / ``product_mismatch`` dicen qué se leyó y qué se esperaba:
  «Leíste 2-1-00-00-000 (el Piso 1 de la Estantería 2); la ruta esperaba
  2-0-00-00-000: estantería.»

Apagada, ninguna de estas funciones se usa y la lectura funciona como antes.
"""

import re

from core import barcode, location, options, traceability

SETTING = options.ROBUST_READS
WINDOW_SETTING = options.DOUBLE_READ_WINDOW
DEFAULT_WINDOW = 1.5
WINDOW_RANGE = (0.2, 10.0)

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_AIM_PREFIX = re.compile(r"^\][A-Za-z][0-9A-Za-z]")
_STEP_KINDS = {"estanteria": "estantería", "piso": "piso", "contenedor": "contenedor", "caja": "caja"}


def enabled(storage) -> bool:
    return options.enabled(storage, SETTING)


def window_seconds(storage) -> float:
    return options.number(storage, WINDOW_SETTING, DEFAULT_WINDOW, *WINDOW_RANGE)


def set_window(storage, actor: dict, value):
    return options.set_number(storage, actor, WINDOW_SETTING, value, *WINDOW_RANGE)


def normalize_code(raw) -> str:
    """Código limpio y en mayúsculas, tal como debe compararse."""
    text = "".join(_CONTROL.sub("", str(raw or "")).split())
    text = _AIM_PREFIX.sub("", text)
    if len(text) > 2 and text.startswith("*") and text.endswith("*"):
        text = text[1:-1]
    return text.upper()


def prepare(storage, raw) -> str:
    """Lo que se valida: normalizado con la opción activa; si no, como antes (solo sin espacios alrededor)."""
    return normalize_code(raw) if enabled(storage) else str(raw or "").strip()


def same_code(expected, read) -> bool:
    return bool(read) and normalize_code(expected) == normalize_code(read)


def is_double_read(last: dict, code: str, now, window: float) -> bool:
    """True si ``code`` es la misma lectura que ``last`` (``{"code", "at"}``) hace menos de ``window`` segundos."""
    if not last or not code or normalize_code(last.get("code")) != normalize_code(code):
        return False
    previous, current = traceability.to_datetime(last.get("at")), traceability.to_datetime(now)
    if previous is None or current is None:
        return False
    return 0 <= (current - previous).total_seconds() < window


def describe_code(code) -> str:
    """Qué es un código del laboratorio, en palabras («el Piso 1 de la Estantería 2»); '' si no se reconoce."""
    text = normalize_code(code)
    place = barcode.location_code(text)
    if place:
        return barcode.location_phrase(place)
    parsed = location.navigable_code(text)
    if not parsed or parsed.get("format") != barcode.FORMAT_STANDARD:
        return ""
    base = f"Estantería {parsed['estanteria']}, Piso {parsed['piso']}"
    container = f"Contenedor {parsed['contenedor']:02d}"
    if parsed.get("item"):
        return f"un producto del {container} ({base})"
    if parsed.get("caja"):
        return f"la Caja {parsed['caja']:02d} del {container} ({base})"
    return f"el {container} ({base})"


def _read_part(code: str) -> str:
    what = describe_code(code)
    return f"Leíste {code}" + (f" ({what})" if what else "")


def route_mismatch(read_code: str, point: dict) -> str:
    """Mensaje accionable de una lectura que no es el punto de la ruta que sigue."""
    kind = _STEP_KINDS.get((point or {}).get("key"), (point or {}).get("title") or "el punto de la ruta")
    return f"{_read_part(read_code)}; la ruta esperaba {(point or {}).get('label_code')}: {kind}."


def product_mismatch(read_code: str, request: dict) -> str:
    """Mensaje accionable de una etiqueta que no es la del producto del pedido."""
    request = request or {}
    name = request.get("item_name") or request.get("item_id") or "el producto solicitado"
    return f"{_read_part(read_code)}; el pedido esperaba {request.get('item_id')}: producto «{name}»."
