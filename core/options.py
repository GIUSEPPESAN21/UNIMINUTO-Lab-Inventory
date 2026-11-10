# -*- coding: utf-8 -*-
"""core/options.py - Opciones de las mejoras nuevas (hoja ``settings``).

Cada mejora va detrás de una opción cuyo valor por defecto es el comportamiento
anterior: apagada. Solo el administrador las cambia (Despacho → Ajustes) y la hoja
``settings`` guarda quién y cuándo. Un valor ilegible nunca activa una opción ni
frena la operación.

Las dos opciones anteriores siguen en sus módulos: ``conteo_ciego``
(core/inventory_count.py) y ``exigir_lectura_caja`` (core/pick_route.py).
"""

from core import permissions

EVENT_LOG = "registro_eventos"               # M3: registro de eventos único y exportable
NFC_AUDIT = "nfc_auditoria"                  # M4: contador/UID de NTAG21x, toques rechazados y salud de chips
NFC_STRICT = "nfc_modo_estricto"             # M4: además, bloquea el toque con contador o UID anómalo
ROBUST_READS = "lectura_robusta"             # M5: anti doble lectura, normalización y mensajes accionables
DOUBLE_READ_WINDOW = "ventana_doble_lectura"  # M5: segundos (número, no interruptor; 1,5 por omisión)
COUNT_BY_LOCATION = "conteo_por_ubicacion"   # M6: continuidad y control del conteo por ubicación
RESEARCH_METRICS = "metricas_investigacion"  # M7: métricas de la lúdica (solo lectura)
NFC_SESSION_PERSISTENT = "nfc_sesion_persistente"   # cookie persistente + espera del puente + versión global

# clave -> (título, explicación) para Despacho → Ajustes.
FLAGS = {
    EVENT_LOG: (
        "Registro de eventos único (lúdica)",
        "Sella cada evento con la sesión y el escenario activos, guarda cada lectura del picking (correcta, "
        "incorrecta, repetida o deshecha) y permite exportar el registro por sesión.",
    ),
    NFC_AUDIT: (
        "Auditoría de chips NFC",
        "Registra los toques rechazados (firma inválida o clave desconocida), revisa el contador y el UID que "
        "envían los NTAG21x grabados con espejo y muestra la salud de los chips (solo administrador).",
    ),
    NFC_STRICT: (
        "Modo estricto NFC",
        "Con la auditoría activa, además bloquea el toque cuando el contador no crece, el UID no es el del chip o "
        "falta el espejo de un chip que ya lo enviaba.",
    ),
    ROBUST_READS: (
        "Lectura robusta (pistola, NFC y teclado)",
        "Limpia lo que manda la pistola antes de validar (espacios, saltos de línea, prefijos y mayúsculas), "
        "ignora la misma lectura repetida dentro de la ventana indicada y explica qué se leyó y qué se esperaba.",
    ),
    COUNT_BY_LOCATION: (
        "Conteo por ubicación (inventario con el teléfono)",
        "Muestra el avance por ubicación (contadas y pendientes), retoma el conteo tras recargar la página y no "
        "deja contar dos veces una ubicación terminada salvo que un supervisor autorice el reconteo.",
    ),
    RESEARCH_METRICS: (
        "Métricas para la investigación",
        "Pestaña «Métricas» en Despacho (solo administrador y solo lectura): exactitud de registros, tiempo por "
        "transacción y lecturas fallidas por escenario y por sesión, con exportación CSV.",
    ),
    NFC_SESSION_PERSISTENT: (
        "Sesión persistente en toques NFC",
        "La cookie de sesión dura hasta el máximo de la sesión (en vez de borrarse al cerrar la pestaña) y, si "
        "un toque NFC abre una pestaña nueva, la app espera un instante a que el puente de la cookie conteste "
        "antes de pedir el inicio de sesión. Un administrador puede invalidar todas las sesiones persistentes "
        "a la vez subiendo el Secret SESSION_TOKEN_VERSION (p. ej. de 1 a 2). El toque NFC solo navega al "
        "artículo: nunca autentica por sí mismo, eso siempre depende de esta cookie.",
    ),
}

_ON = {"1", "true", "si", "sí", "yes", "on", "activado"}


def enabled(storage, key: str) -> bool:
    """¿La opción ``key`` está activa? No por omisión ni si no se puede leer."""
    try:
        value = str(storage.get_setting(key, "0") or "").strip().lower()
    except Exception:
        value = "0"
    return value in _ON


def set_enabled(storage, actor: dict, key: str, on: bool):
    """Activa o apaga una opción (solo administrador). Devuelve (ok, mensaje)."""
    permissions.require_cap(actor, permissions.CAP_DISPATCH_SETTINGS, "Solo un administrador cambia esta opción.")
    if key not in FLAGS:
        return False, "Opción desconocida."
    storage.set_setting(key, "1" if on else "0", (actor or {}).get("institutional_email", ""))
    return True, f"{FLAGS[key][0]}: {'activado' if on else 'desactivado'}."


def number(storage, key: str, default: float, low: float, high: float) -> float:
    """Número guardado en ``settings`` dentro de [low, high]; ``default`` si falta o no se puede leer."""
    try:
        value = float(str(storage.get_setting(key, "") or "").strip().replace(",", "."))
    except Exception:
        return default
    return value if low <= value <= high else default


def set_number(storage, actor: dict, key: str, value, low: float, high: float):
    """Guarda un número de configuración (solo administrador). Devuelve (ok, mensaje)."""
    permissions.require_cap(actor, permissions.CAP_DISPATCH_SETTINGS, "Solo un administrador cambia esta opción.")
    try:
        number_value = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return False, "Escribe un número."
    if not low <= number_value <= high:
        return False, f"El valor debe estar entre {low:g} y {high:g}."
    storage.set_setting(key, f"{number_value:g}", (actor or {}).get("institutional_email", ""))
    return True, f"Valor guardado: {number_value:g}."
