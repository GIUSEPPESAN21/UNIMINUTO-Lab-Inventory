# -*- coding: utf-8 -*-
"""core/research_metrics.py - Métricas para el análisis de la lúdica (M7, solo lectura).

Opción ``metricas_investigacion`` (Despacho → Ajustes, apagada por omisión). Activada,
el administrador ve la pestaña «Métricas» en Despacho. Todo se calcula con las filas
del registro de eventos (core/event_log.rows): no se escribe nada.

Por escenario (A/B/C) y por sesión de la lúdica:

- ``exactitud_registros`` = registros correctos / registros evaluados (filas con
  ``resultado_sistema`` ok o rechazado; las repetidas ignoradas y las deshechas no
  cuentan).
- ``lecturas`` y ``lecturas_fallidas``: intentos de lectura del picking
  (correcta, incorrecta, reintento y, en datos anteriores a M3, las lecturas
  rechazadas). Los toques NFC no se suman aparte: en el picking ya llegan como
  lectura. ``exactitud_lecturas`` = lecturas correctas / lecturas.
- ``transacciones``: pedidos con picking completo; ``tiempo_picking_promedio_s`` y
  ``tiempo_total_promedio_s``: promedio de picking_seconds y delivery_seconds,
  recalculados del registro (los mismos valores del reporte de Tiempos). Cada pedido
  cuenta en la sesión y el escenario de su último picking.
"""

import csv
import io

from core import event_log, excel_safe, options, permissions, traceability

SETTING = options.RESEARCH_METRICS
ALL_SESSIONS = "(todas)"
COLUMNS = (
    "sesion_id", "escenario", "registros", "registros_correctos", "exactitud_registros", "lecturas",
    "lecturas_fallidas", "exactitud_lecturas", "transacciones", "tiempo_picking_promedio_s",
    "tiempo_total_promedio_s",
)
READ_TYPES = frozenset(event_log.READ_KINDS) | {traceability.EVENT_ORDER_SCAN_FAILED}
_EVALUATED = (event_log.RESULT_OK, event_log.RESULT_REJECTED)


def enabled(storage) -> bool:
    return options.enabled(storage, SETTING)


def _ratio(part: int, whole: int):
    return round(part / whole, 4) if whole else ""


def _mean(values: list):
    values = [value for value in values if value != ""]
    return round(sum(values) / len(values), 1) if values else ""


def summarize(log_rows: list, by_session: bool = True) -> list:
    """Una fila por (sesión, escenario); con ``by_session=False``, una por escenario (sesión «(todas)»)."""
    groups, picked_in = {}, {}
    for row in log_rows or []:
        key = (row.get("sesion_id", "") if by_session else ALL_SESSIONS, row.get("escenario", ""))
        group = groups.setdefault(key, {"records": 0, "correct": 0, "reads": 0, "failed": 0})
        verdict, kind = row.get("resultado_sistema"), row.get("tipo_evento")
        if verdict in _EVALUATED:
            group["records"] += 1
            group["correct"] += 1 if verdict == event_log.RESULT_OK else 0
        if kind in READ_TYPES and verdict in _EVALUATED:
            group["reads"] += 1
            group["failed"] += 1 if verdict == event_log.RESULT_REJECTED else 0
        if kind == traceability.EVENT_ORDER_PICKED and verdict == event_log.RESULT_OK:
            picked_in[row.get("transaccion_id", "")] = key        # las filas vienen en orden: gana el último
    times = event_log.times_from_rows(log_rows)
    members = {}
    for transaction, key in picked_in.items():
        members.setdefault(key, []).append(transaction)
    result = []
    for key in sorted(groups):
        group, done = groups[key], members.get(key, [])
        result.append({
            "sesion_id": key[0], "escenario": key[1],
            "registros": group["records"], "registros_correctos": group["correct"],
            "exactitud_registros": _ratio(group["correct"], group["records"]),
            "lecturas": group["reads"], "lecturas_fallidas": group["failed"],
            "exactitud_lecturas": _ratio(group["reads"] - group["failed"], group["reads"]),
            "transacciones": len(done),
            "tiempo_picking_promedio_s": _mean([times.get(t, {}).get("picking_seconds", "") for t in done]),
            "tiempo_total_promedio_s": _mean([times.get(t, {}).get("delivery_seconds", "") for t in done]),
        })
    return result


def report(storage, actor: dict) -> dict:
    """``{"by_scenario", "by_session"}`` para la pestaña del administrador (solo lectura)."""
    permissions.require_cap(actor, permissions.CAP_DISPATCH_SETTINGS, "Solo un administrador ve las métricas.")
    rows = event_log.rows(storage)
    return {"by_scenario": summarize(rows, by_session=False), "by_session": summarize(rows, by_session=True)}


def csv_bytes(storage, actor: dict) -> bytes:
    """CSV (UTF-8, sin BOM): primero por escenario (sesión «(todas)»), después por sesión y escenario."""
    data = report(storage, actor)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(COLUMNS), lineterminator="\n")
    writer.writeheader()
    for row in data["by_scenario"] + data["by_session"]:
        writer.writerow({key: excel_safe.csv_safe(value) for key, value in row.items()})
    return output.getvalue().encode("utf-8")
