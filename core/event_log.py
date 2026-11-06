# -*- coding: utf-8 -*-
"""core/event_log.py - Registro de eventos único, ordenado y exportable (M3).

Opción ``registro_eventos`` (Despacho → Ajustes, apagada por omisión). Activada:

- Cada evento que se escribe en ``trace_events`` y cada solicitud nueva quedan
  sellados EN ESE MOMENTO con la sesión de la lúdica (columna ``ludica_session``)
  y el escenario activo (columna ``scenario``: A manual, B barras, C NFC). La hora
  oficial es siempre la del servidor (``created_at``, UTC); nada viene del celular
  ni del navegador.
- Cada lectura del picking (correcta, incorrecta, repetida o deshecha) se anota en
  la memoria del proceso con la hora del servidor (core/picking.py) y se guarda en
  la MISMA escritura que completa, libera o reasigna el pedido
  (``details.read_log``): no hay una escritura a GitHub por lectura.
- ``rows()`` arma el registro único: una fila por hecho, ordenada por hora, con las
  columnas de la lúdica y ``escenario`` al final. ``times_from_rows()`` recalcula
  desde esas filas las columnas de Tiempos: deben coincidir con
  ``core.dispatch.dispatch_metrics`` (las dos leen los mismos eventos).

Apagada, no se sella ni se guarda nada nuevo y los CSV salen igual que antes.

Valores de ``tipo_evento`` además de los tipos de evento existentes:
``lectura_correcta``, ``lectura_incorrecta``, ``reintento`` (lectura tras una
rechazada), ``lectura_repetida``, ``lectura_deshecha`` y ``confirmacion_manual``.
``resultado_sistema``: ``ok``, ``rechazado``, ``ignorado`` (repetida) o
``deshecho``.
"""

import csv
import io
from datetime import datetime, timezone

from core import dispatch, excel_safe, ludica, options, permissions, picking, traceability
from core.reservations import as_bogota

SETTING = options.EVENT_LOG
COLUMNS = ludica.COLUMNS + ("escenario",)
TIME_COLUMNS = ("transaccion_id", "sesion_id", "escenario")   # se agregan al FINAL del CSV de Tiempos
ZONE_LABEL = "America/Bogota"

READ_OK = "lectura_correcta"
READ_REJECTED = "lectura_incorrecta"
READ_RETRY = "reintento"
READ_DUPLICATE = "lectura_repetida"
READ_UNDONE = "lectura_deshecha"
MANUAL_CONFIRMATION = "confirmacion_manual"
READ_KINDS = (READ_OK, READ_REJECTED, READ_RETRY)            # intentos que el sistema evaluó
RESULT_OK, RESULT_REJECTED = ludica.RESULT_OK, ludica.RESULT_REJECTED
RESULT_IGNORED, RESULT_UNDONE = "ignorado", "deshecho"

# Resultado de cada lectura anotada en core/picking (``result`` de cada entrada).
LOG_OK, LOG_REJECTED, LOG_DUPLICATE, LOG_UNDONE = (
    picking.LOG_OK, picking.LOG_REJECTED, picking.LOG_DUPLICATE, picking.LOG_UNDONE)

_STAGE_EVENTS = traceability.DISPATCH_LIFECYCLE_EVENTS - {traceability.EVENT_ORDER_REQUESTED}
_OK_EVENTS = frozenset({
    traceability.EVENT_NFC_TAP, traceability.EVENT_PICKED_UP, traceability.EVENT_ROUTE_VERIFIED,
    traceability.EVENT_COUNT_STARTED, traceability.EVENT_COUNT_MARK, traceability.EVENT_COUNT_CLOSED,
    *traceability.RETURN_EVENTS, *traceability.PIECE_EVENTS,
})
_REJECTED_EVENTS = frozenset()       # tipos de evento que son en sí un intento rechazado (los agrega M4)
_AWARE_MIN = datetime.min.replace(tzinfo=timezone.utc)


def enabled(storage) -> bool:
    return options.enabled(storage, SETTING)


def set_enabled(storage, actor: dict, on: bool):
    return options.set_enabled(storage, actor, SETTING, on)


def stamp_fields(storage) -> dict:
    """``{"ludica_session", "scenario"}`` para sellar un evento al escribirlo; ``{}`` con la opción apagada.
    Nunca lanza: un ajuste ilegible no impide escribir el evento."""
    try:
        if not enabled(storage):
            return {}
        return {"ludica_session": ludica.session_id(storage), "scenario": dispatch.scenario_key(storage)}
    except Exception:
        return {}


def read_entry(storage, actor: dict, code: str, method: str, result: str, step: str = "", now=None) -> dict:
    """Entrada del registro de lecturas de un picking, con la hora del SERVIDOR y el sello del momento."""
    moment = traceability.to_datetime(now) or datetime.now(timezone.utc)
    actor = actor or {}
    return {
        "at": moment.isoformat(timespec="milliseconds"), "code": str(code or "").strip(), "method": method or "",
        "result": result, "step": step or "", "actor_id": actor.get("id", ""), "role": actor.get("role", ""),
        **stamp_fields(storage),
    }


# ---------------------------------------------------------------------------
# Filas del registro
# ---------------------------------------------------------------------------

def _timestamp(value) -> str:
    """ISO en UTC con toda la precisión guardada (los Tiempos se recalculan sin redondeos)."""
    moment = traceability.to_datetime(value)
    return moment.astimezone(timezone.utc).isoformat() if moment else ""


def _row(sid, scenario, when, role, kind, item, transaction, actor_id, result, salt) -> dict:
    return {
        "sesion_id": sid or "", "timestamp": _timestamp(when), "estacion": ludica.station_of(role),
        "tipo_evento": kind, "item_codigo": item or "", "transaccion_id": ludica.pseudonym(transaction, salt),
        "usuario_sim": ludica.pseudonym(actor_id, salt), "resultado_sistema": result, "escenario": scenario or "",
    }


def _read_rows(log, sid: str, scenario: str, item: str, request_id: str, salt: str) -> list:
    rows, rejected_before = [], False
    for entry in log if isinstance(log, list) else []:
        if not isinstance(entry, dict):
            continue
        result = entry.get("result")
        if result == LOG_DUPLICATE:
            kind, verdict = READ_DUPLICATE, RESULT_IGNORED
        elif result == LOG_UNDONE:
            kind, verdict = READ_UNDONE, RESULT_UNDONE
        else:
            accepted = result == LOG_OK
            kind = READ_RETRY if rejected_before else (READ_OK if accepted else READ_REJECTED)
            verdict = RESULT_OK if accepted else RESULT_REJECTED
            rejected_before = not accepted
        rows.append(_row(entry.get("ludica_session") or sid, entry.get("scenario") or scenario, entry.get("at"),
                         entry.get("role"), kind, entry.get("code") or item, request_id, entry.get("actor_id"),
                         verdict, salt))
    return rows


def _transaction(event: dict, details: dict) -> str:
    if event.get("event_type") == traceability.EVENT_COUNT_STARTED:
        return str(event.get("id") or "")
    return str(event.get("request_id") or event.get("loan_id") or details.get("session")
               or details.get("count_session") or "")


def event_rows(event: dict, salt: str = ludica.DEFAULT_SALT) -> list:
    """Filas del registro que aporta UN evento guardado (puede ser ninguna)."""
    kind, details = event.get("event_type"), traceability.event_details(event)
    sid, scenario = event.get("ludica_session") or "", event.get("scenario") or ""
    item, request_id = event.get("item_id"), event.get("request_id") or ""
    role, actor = event.get("actor_role"), event.get("actor_id")
    transaction = _transaction(event, details)
    rows = []
    if kind in _STAGE_EVENTS or kind in _OK_EVENTS:
        rows.append(_row(sid, scenario, event.get("created_at"), role, kind, item, transaction, actor, RESULT_OK, salt))
    elif kind in _REJECTED_EVENTS:
        rows.append(_row(sid, scenario, event.get("created_at"), role, kind, item, transaction, actor,
                         RESULT_REJECTED, salt))
    elif kind == traceability.EVENT_ORDER_ACTION_FAILED:
        attempted = str(details.get("attempted_event") or kind)
        rows.append(_row(sid, scenario, event.get("created_at"), role, attempted, item, transaction, actor,
                         RESULT_REJECTED, salt))
    elif kind == traceability.EVENT_ORDER_SCAN_FAILED and not details.get("in_read_log"):
        scans = details.get("scans") or [{"code": details.get("scanned_code"), "at": event.get("created_at")}]
        for scan in scans:
            rows.append(_row(sid, scenario, scan.get("at") or event.get("created_at"), role, kind,
                             scan.get("code") or item, transaction, actor, RESULT_REJECTED, salt))
    rows.extend(_read_rows(details.get("read_log"), sid, scenario, item, request_id, salt))
    manual = details.get("manual_confirmation")
    if kind == traceability.EVENT_ORDER_PICKED and isinstance(manual, dict):
        rows.append(_row(sid, scenario, manual.get("at") or event.get("created_at"), role, MANUAL_CONFIRMATION,
                         item, transaction, manual.get("operator_id") or actor, RESULT_OK, salt))
    return rows


def request_stamp(request: dict, events: list) -> tuple:
    """(sesión, escenario) de una solicitud: los sellados al crearla o, si se creó con la opción apagada (p. ej.
    sembrada antes de la sesión), los del primer evento sellado del pedido."""
    sid, scenario = (request or {}).get("ludica_session") or "", (request or {}).get("scenario") or ""
    if sid:
        return sid, scenario
    stamped = next((event for event in events or [] if event.get("ludica_session")), None)
    return ((stamped.get("ludica_session") or "", stamped.get("scenario") or "") if stamped else ("", ""))


def rows(storage, session: str = None, salt: str = ludica.DEFAULT_SALT) -> list:
    """Registro único ordenado por hora del servidor. ``session``: solo las filas de esa sesión de la lúdica."""
    events = storage.get_trace_events()
    by_request = {}
    for event in events:
        if event.get("request_id"):
            by_request.setdefault(event["request_id"], []).append(event)
    ordered = []
    for request in storage.get_service_requests():
        if request.get("request_type") != "product":
            continue
        sid, scenario = request_stamp(request, by_request.get(request.get("id"), []))
        ordered.append((-1, _row(sid, scenario, request.get("created_at"), permissions.ROLE_STUDENT,
                                 traceability.EVENT_ORDER_REQUESTED, request.get("item_id"), request.get("id"),
                                 request.get("requester_id"), RESULT_OK, salt)))
    for position, event in enumerate(events):
        ordered.extend((position, row) for row in event_rows(event, salt))
    if session is not None:
        ordered = [(position, row) for position, row in ordered if row["sesion_id"] == session]
    ordered.sort(key=lambda pair: (traceability.to_datetime(pair[1]["timestamp"]) or _AWARE_MIN, pair[0]))
    return [row for _, row in ordered]


def sessions(storage) -> list:
    """Sesiones de la lúdica selladas en eventos o solicitudes, en orden alfabético (sin leer los detalles)."""
    found = {str(event.get("ludica_session") or "") for event in storage.get_trace_events()}
    found |= {str(request.get("ludica_session") or "") for request in storage.get_service_requests()}
    found.discard("")
    return sorted(found)


def csv_bytes(storage, actor: dict, session: str = None, salt: str = ludica.DEFAULT_SALT) -> bytes:
    """CSV del registro (UTF-8, sin BOM): columnas de la lúdica y ``escenario`` al final."""
    permissions.require_cap(actor, permissions.CAP_DISPATCH_REPORTS)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(COLUMNS), lineterminator="\n")
    writer.writeheader()
    for row in rows(storage, session, salt):
        writer.writerow({key: excel_safe.csv_safe(value) for key, value in row.items()})
    return output.getvalue().encode("utf-8")


def bogota_text(timestamp) -> str:
    """Hora del registro para mostrar, con la zona explícita: «2026-10-09 10:00:00.123 (America/Bogota)»."""
    moment = traceability.to_datetime(timestamp)
    if moment is None:
        return ""
    local = as_bogota(moment)
    return f"{local.strftime('%Y-%m-%d %H:%M:%S')}.{local.microsecond // 1000:03d} ({ZONE_LABEL})"


# ---------------------------------------------------------------------------
# Tiempos derivados del registro
# ---------------------------------------------------------------------------

_TIME_MARKS = (traceability.EVENT_ORDER_REQUESTED, traceability.EVENT_ORDER_CLAIMED,
               traceability.EVENT_ORDER_PICKED, traceability.EVENT_ORDER_DELIVERED)


def _seconds(start, end):
    return int((end - start).total_seconds()) if start and end else ""


def times_from_rows(log_rows: list) -> dict:
    """``{transaccion_id: {claim_seconds, picking_seconds, delivery_seconds}}`` calculado SOLO con las filas del
    registro (el último evento de cada tipo, como el reporte de Tiempos)."""
    marks = {}
    for row in log_rows or []:
        if row.get("tipo_evento") in _TIME_MARKS and row.get("resultado_sistema") == RESULT_OK:
            marks.setdefault(row["transaccion_id"], {})[row["tipo_evento"]] = traceability.to_datetime(row["timestamp"])
    requested, claimed, picked, delivered = _TIME_MARKS
    return {
        transaction: {
            "claim_seconds": _seconds(found.get(requested), found.get(claimed)),
            "picking_seconds": _seconds(found.get(claimed), found.get(picked)),
            "delivery_seconds": _seconds(found.get(requested), found.get(delivered)),
        }
        for transaction, found in marks.items()
    }


def time_columns(request: dict, events: list, salt: str = ludica.DEFAULT_SALT) -> dict:
    """Columnas que el CSV de Tiempos agrega al final con la opción activa: el mismo ``transaccion_id`` del
    registro (para cruzar los dos archivos), la sesión y el escenario."""
    sid, scenario = request_stamp(request, events)
    return {"transaccion_id": ludica.pseudonym((request or {}).get("id"), salt), "sesion_id": sid,
            "escenario": scenario}
