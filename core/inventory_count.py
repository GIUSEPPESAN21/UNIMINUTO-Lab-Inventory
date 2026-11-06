# -*- coding: utf-8 -*-
"""core/inventory_count.py - Inventario fisico con el telefono.

Un profesor o el maestro abre un conteo (todo el inventario, una estanteria o
un contenedor). Mientras esta abierto, cada toque de chip NFC de un profesor o
del maestro (o el codigo escrito/escaneado en la pagina del conteo) marca el
producto como verificado, opcionalmente con la cantidad contada en el estante.
Al cerrar se guarda UN resumen: esperados, verificados, faltantes y diferencias.

Las cantidades del inventario NO cambian al contar. Solo el maestro puede
aplicar ajustes, eligiendo cada diferencia y confirmando de forma explicita;
cada ajuste queda en el historial del producto.

Todo vive en la hoja `trace_events` (no hay hoja nueva):
- count_started: abre el conteo; su id es el id del conteo.
- nfc_tap con details.count_session: toque de un chip durante el conteo.
- count_mark: codigo escrito/escaneado y/o cantidad contada.
- count_closed: resumen al cerrar.

Logica pura (sin Streamlit): se prueba con FakeStorage.
"""

import json
import re
from datetime import datetime, timezone

from core import barcode, location, permissions, traceability
from core.reservations import as_bogota

SCOPE_ALL = "all"
SCOPE_SHELF = "shelf"
SCOPE_CONTAINER = "container"
SCOPE_KINDS = (SCOPE_ALL, SCOPE_SHELF, SCOPE_CONTAINER)

METHOD_NFC = "nfc"
METHOD_CODE = "codigo"
METHOD_LABELS = {METHOD_NFC: "Chip NFC", METHOD_CODE: "Código"}

MAX_LIST = 500  # tope de codigos guardados por lista en el resumen (la celda del Excel es finita)

# Conteo ciego (v1.23, apagado por omision): mientras el conteo esta abierto, quien
# cuenta no ve cuanto "deberia haber" (evita que ajuste el conteo a lo esperado).
# El resumen al cerrar siempre muestra esperado, contado y diferencia: ver close_session.
SETTING_BLIND_COUNT = "conteo_ciego"
_BLIND_ON = {"1", "true", "si", "sí", "yes", "on", "activado"}


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _now(now=None) -> datetime:
    return traceability.to_datetime(now) or datetime.now(timezone.utc)


def _actor_fields(actor: dict) -> dict:
    actor = actor or {}
    return {
        "actor_id": actor.get("id", ""), "actor_name": actor.get("full_name", ""),
        "actor_email": actor.get("institutional_email", ""),
        "actor_role": actor.get("role", ""),
    }


def _event(kind: str, actor: dict, details: dict, item_id: str = "", now=None) -> dict:
    return {
        "event_type": kind, "request_id": "", "item_id": item_id, "loan_id": "",
        "user_id": (actor or {}).get("id", ""), **_actor_fields(actor), "receipt": "",
        "details": json.dumps(details, ensure_ascii=False), "created_at": _now(now).isoformat(),
    }


def code_order(code) -> tuple:
    """Orden natural por codigo (2-1-02 antes que 2-1-10)."""
    parts = re.split(r"(\d+)", str(code or ""))
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p.lower()) for p in parts if p)


def parse_quantity(value):
    """Cantidad contada: None (no se conto) o entero >= 0. Lanza ValueError."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError("La cantidad contada debe ser un número entero.")
    try:
        number = float(str(value).strip())
    except ValueError:
        raise ValueError("La cantidad contada debe ser un número entero.") from None
    if not number.is_integer() or number < 0:
        raise ValueError("La cantidad contada debe ser un número entero mayor o igual a 0.")
    return int(number)


# ---------------------------------------------------------------------------
# Alcance del conteo
# ---------------------------------------------------------------------------

def normalize_scope(scope) -> dict:
    """{"kind": all} | {"kind": shelf, "estanteria": n} | {"kind": container, "id": codigo}."""
    scope = dict(scope or {})
    kind = scope.get("kind") or SCOPE_ALL
    if kind == SCOPE_SHELF:
        try:
            shelf = int(scope.get("estanteria"))
        except (TypeError, ValueError):
            raise ValueError("Elige la estantería que vas a contar.") from None
        if not 1 <= shelf <= 99:
            raise ValueError("La estantería no es válida.")
        return {"kind": SCOPE_SHELF, "estanteria": shelf}
    if kind == SCOPE_CONTAINER:
        container = str(scope.get("id") or "").strip()
        if not container:
            raise ValueError("Elige el contenedor que vas a contar.")
        return {"kind": SCOPE_CONTAINER, "id": container, "name": str(scope.get("name") or "")}
    if kind != SCOPE_ALL:
        raise ValueError("Alcance de conteo no válido.")
    return {"kind": SCOPE_ALL}


def describe_scope(scope) -> str:
    scope = scope or {}
    if scope.get("kind") == SCOPE_SHELF:
        return f"Estantería {scope.get('estanteria')}"
    if scope.get("kind") == SCOPE_CONTAINER:
        name = scope.get("name")
        return f"Contenedor «{name}» ({scope.get('id')})" if name else f"Contenedor {scope.get('id')}"
    return "Todo el inventario"


def shelf_of(code: str):
    """Estanteria de un codigo GLIOPS (estandar o Lego); None si no tiene."""
    parsed = location.navigable_code(code)
    if not parsed or parsed.get("format") == barcode.FORMAT_MESA:
        return None
    return parsed.get("estanteria")


def item_in_scope(item: dict, scope: dict, parent: dict = None) -> bool:
    """¿El producto entra en el conteo? Los contenedores (maestros) son lugares,
    no se cuentan; los dados de baja tampoco."""
    item = item or {}
    if item.get("item_type") == "master" or item.get("status") == "retired":
        return False
    kind = (scope or {}).get("kind") or SCOPE_ALL
    if kind == SCOPE_CONTAINER:
        return str(item.get("parent_id") or "") == str(scope.get("id") or "")
    if kind == SCOPE_SHELF:
        shelf = shelf_of(str(item.get("id") or "")) or shelf_of(str((parent or {}).get("id") or ""))
        return shelf == scope.get("estanteria")
    return True


def expected_items(items: list, scope: dict) -> list:
    by_id = {str(item.get("id")): item for item in items or []}
    rows = [item for item in items or [] if item_in_scope(item, scope, by_id.get(str(item.get("parent_id") or "")))]
    return sorted(rows, key=lambda item: code_order(item.get("id")))


def resolve_code(code, items: list):
    """Producto con ese codigo (exacto; si no, tolerando mayusculas y ceros de
    relleno de los codigos GLIOPS, como al escribir a mano)."""
    code = str(code or "").strip()
    if not code:
        return None
    for item in items or []:
        if str(item.get("id")) == code:
            return item
    for item in items or []:
        if location.codes_match(str(item.get("id") or ""), code):
            return item
    return None


# ---------------------------------------------------------------------------
# Sesiones de conteo
# ---------------------------------------------------------------------------

def session_from_event(event: dict) -> dict:
    info = traceability.event_details(event)
    return {
        "id": event.get("id", ""), "name": info.get("name") or "Conteo",
        "scope": info.get("scope") or {"kind": SCOPE_ALL}, "started_at": event.get("created_at", ""),
        "started_by": event.get("actor_name", ""), "started_by_id": event.get("actor_id", ""),
    }


def _closed_ids(storage) -> set:
    return {
        traceability.event_details(event).get("session")
        for event in storage.get_trace_events(event_type=traceability.EVENT_COUNT_CLOSED)
    }


def open_session(storage):
    """El conteo abierto (el mas reciente sin cerrar) o None."""
    started = storage.get_trace_events(event_type=traceability.EVENT_COUNT_STARTED)
    if not started:
        return None
    closed = _closed_ids(storage)
    for event in reversed(started):
        if event.get("id") not in closed:
            return session_from_event(event)
    return None


def default_name(now=None) -> str:
    return f"Conteo {as_bogota(_now(now)).strftime('%d/%m/%Y')}"


def start_session(storage, actor: dict, name: str = "", scope: dict = None, now=None):
    """Abre un conteo. Devuelve (ok, mensaje, sesion)."""
    if not traceability.is_manager(actor):
        return False, "Solo un supervisor autorizado o un administrador pueden abrir un conteo.", None
    current = open_session(storage)
    if current:
        return False, f"Ya hay un conteo abierto («{current['name']}»): ciérralo antes de abrir otro.", current
    try:
        scope = normalize_scope(scope)
    except ValueError as exc:
        return False, str(exc), None
    name = " ".join(str(name or "").split())[:80] or default_name(now)
    event = storage.add_trace_event(_event(traceability.EVENT_COUNT_STARTED, actor,
                                           {"name": name, "scope": scope}, now=now))
    return True, f"Conteo «{name}» abierto ({describe_scope(scope)}).", session_from_event(event)


def session_marks(storage, session_id: str) -> list:
    """Verificaciones del conteo (toques NFC y codigos/cantidades), en orden de hora."""
    marks = []
    if not session_id:
        return marks
    for event in storage.get_trace_events(event_type=traceability.EVENT_NFC_TAP):
        info = traceability.event_details(event)
        if info.get("count_session") == session_id:
            marks.append({"code": str(event.get("item_id") or info.get("code") or ""), "qty": None,
                          "method": METHOD_NFC, "at": event.get("created_at", ""),
                          "by": event.get("actor_name", ""), "by_id": event.get("actor_id", "")})
    for event in storage.get_trace_events(event_type=traceability.EVENT_COUNT_MARK):
        info = traceability.event_details(event)
        if info.get("session") == session_id and not info.get("duplicate"):
            try:
                qty = parse_quantity(info.get("qty"))
            except ValueError:
                qty = None
            marks.append({"code": str(event.get("item_id") or ""), "qty": qty,
                          "method": info.get("method") or METHOD_CODE, "at": event.get("created_at", ""),
                          "by": event.get("actor_name", ""), "by_id": event.get("actor_id", "")})
    aware_min = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(marks, key=lambda mark: traceability.to_datetime(mark["at"]) or aware_min)


def _mark_qty(event: dict):
    try:
        return parse_quantity(traceability.event_details(event).get("qty"))
    except ValueError:
        return None


def _last_mark(storage, session_id: str, actor: dict):
    """Última marca real (no repetida) de ``actor`` en ese conteo, o ``None``."""
    marks = [event for event in storage.get_trace_events(user_id=(actor or {}).get("id", ""),
                                                         event_type=traceability.EVENT_COUNT_MARK)
             if traceability.event_details(event).get("session") == session_id
             and not traceability.event_details(event).get("duplicate")]
    return marks[-1] if marks else None


def record_mark(storage, session: dict, actor: dict, code: str, qty=None, method: str = METHOD_CODE,
                item: dict = None, now=None):
    """Marca `code` como verificado en el conteo (y la cantidad contada, si se da).
    Devuelve (ok, mensaje, evento). No cambia la cantidad del inventario."""
    if not traceability.is_manager(actor):
        return False, "Solo un supervisor autorizado o un administrador pueden contar.", None
    if not session or not session.get("id"):
        return False, "No hay un conteo abierto.", None
    code = str(code or "").strip()
    if not code:
        return False, "Escribe o escanea el código del producto.", None
    try:
        qty = parse_quantity(qty)
    except ValueError as exc:
        return False, str(exc), None
    if method not in METHOD_LABELS:
        method = METHOD_CODE
    if by_location_enabled(storage):
        blocked = location_blocked(storage, session, item or storage.get_item(code))
        if blocked:
            return False, blocked, None
    from core import reading      # diferido: reading depende de options

    if reading.enabled(storage):
        window = reading.window_seconds(storage)
        last = _last_mark(storage, session["id"], actor)
        if (last and reading.is_double_read({"code": last.get("item_id"), "at": last.get("created_at")}, code,
                                            _now(now), window)
                and _mark_qty(last) == qty):
            details = {"session": session["id"], "qty": qty, "method": method,
                       "item_name": (item or {}).get("name", ""), "duplicate": True}
            event = storage.add_trace_event(_event(traceability.EVENT_COUNT_MARK, actor, details, item_id=code,
                                                   now=now))
            seconds = f"{window:g}".replace(".", ",")
            return True, (f"Lectura repetida ignorada: «{(item or {}).get('name') or code}» llegó dos veces en menos "
                          f"de {seconds} s."), event
    details = {"session": session["id"], "qty": qty, "method": method, "item_name": (item or {}).get("name", "")}
    event = storage.add_trace_event(_event(traceability.EVENT_COUNT_MARK, actor, details, item_id=code, now=now))
    name = (item or {}).get("name") or code
    message = f"«{name}» verificado" + (f": {qty} u. contadas en el estante." if qty is not None else ".")
    return True, message, event


def _summarize_marks(marks: list) -> dict:
    """Por codigo: metodos usados, ultima cantidad contada, ultima hora y quien."""
    summary = {}
    for mark in marks or []:
        entry = summary.setdefault(mark["code"], {"methods": [], "qty": None, "at": "", "by": "", "marks": 0})
        if mark["method"] not in entry["methods"]:
            entry["methods"].append(mark["method"])
        if mark.get("qty") is not None:
            entry["qty"] = mark["qty"]
        entry.update(at=mark.get("at", ""), by=mark.get("by", ""))
        entry["marks"] += 1
    return summary


def progress(session: dict, items: list, marks: list, availability: dict) -> dict:
    """Avance del conteo.

    Para cada producto esperado: verificado o no, cantidad contada, lo que
    deberia haber en el estante (disponible = total - en prestamo) y la
    diferencia. Ademas los codigos verificados fuera del alcance."""
    scope = (session or {}).get("scope") or {"kind": SCOPE_ALL}
    expected = expected_items(items, scope)
    by_id = {str(item.get("id")): item for item in items or []}
    summary = _summarize_marks(marks)
    rows = []
    for item in expected:
        code = str(item.get("id"))
        quantity = int(item.get("quantity") or 0)
        available = int(availability.get(code, quantity) if availability else quantity)
        mark = summary.get(code)
        counted = mark["qty"] if mark else None
        rows.append({
            "code": code, "name": item.get("name") or code, "location": item.get("location") or "",
            "parent_id": item.get("parent_id") or "", "quantity": quantity, "available": available,
            "on_loan": max(quantity - available, 0), "verified": bool(mark), "counted": counted,
            "difference": (counted - available) if counted is not None else None,
            "methods": list(mark["methods"]) if mark else [], "at": mark["at"] if mark else "",
            "by": mark["by"] if mark else "",
        })
    expected_codes = {row["code"] for row in rows}
    extra = []
    for code, mark in summary.items():
        if code in expected_codes:
            continue
        item = by_id.get(code)
        extra.append({"code": code, "name": (item or {}).get("name") or "", "known": bool(item),
                      "counted": mark["qty"], "methods": list(mark["methods"]), "at": mark["at"], "by": mark["by"]})
    extra.sort(key=lambda row: code_order(row["code"]))
    verified = [row for row in rows if row["verified"]]
    missing = [row for row in rows if not row["verified"]]
    differences = [row for row in verified if row["difference"] not in (None, 0)]
    total = len(rows)
    return {
        "rows": rows, "verified": verified, "missing": missing, "differences": differences, "extra": extra,
        "total": total, "verified_count": len(verified), "missing_count": len(missing),
        "counted_count": len([row for row in verified if row["counted"] is not None]),
        "nfc_count": len([row for row in verified if METHOD_NFC in row["methods"]]),
        "difference_count": len(differences),
        "ratio": (len(verified) / total) if total else 0.0,
    }


# ---------------------------------------------------------------------------
# Ajustes (solo el maestro, explicitos) y cierre
# ---------------------------------------------------------------------------

def can_adjust(actor: dict) -> bool:
    return permissions.has_cap(actor, permissions.CAP_ADJUST_COUNTS)


def blind_count_enabled(storage) -> bool:
    """¿El conteo ciego está activo? No por omisión; un ajuste ilegible nunca lo activa."""
    try:
        value = str(storage.get_setting(SETTING_BLIND_COUNT, "0") or "").strip().lower()
    except Exception:
        value = "0"
    return value in _BLIND_ON


def set_blind_count(storage, actor: dict, on: bool):
    """Activa o apaga el conteo ciego (solo administrador). Devuelve (ok, mensaje)."""
    permissions.require_cap(actor, permissions.CAP_ADJUST_COUNTS, "Solo un administrador activa el conteo ciego.")
    storage.set_setting(SETTING_BLIND_COUNT, "1" if on else "0", (actor or {}).get("institutional_email", ""))
    message = ("Conteo ciego activado: mientras el conteo esté abierto no se muestra la cantidad esperada."
               if on else "Conteo ciego desactivado: vuelve a mostrarse la cantidad esperada durante el conteo.")
    return True, message


NO_PLACE = "Sin ubicación"


def place_of(row: dict, names: dict = None) -> tuple:
    """(clave, nombre) del lugar donde se cuenta un producto: su contenedor o, si no
    tiene, la ubicación escrita. Clave "" = sin ubicación."""
    parent = str((row or {}).get("parent_id") or "").strip()
    if parent:
        name = str((names or {}).get(parent) or "").strip()
        return parent, (f"{name} ({parent})" if name else parent)
    where = " ".join(str((row or {}).get("location") or "").split())
    if where:
        return f"ubicacion:{where.casefold()}", where
    return "", NO_PLACE


def summary_by_location(report: dict, names: dict = None) -> list:
    """Esperado, contado y diferencia por ubicación a partir de ``progress()``.

    ``expected``, ``counted`` y ``difference`` se calculan sobre los productos con
    cantidad anotada (difference = counted - expected); ``expected_total`` es lo que
    el sistema esperaba en el estante para TODOS los productos de esa ubicación."""
    groups = {}
    for row in (report or {}).get("rows") or []:
        key, label = place_of(row, names)
        group = groups.setdefault(key, {
            "location": label, "items": 0, "verified": 0, "with_quantity": 0,
            "expected": 0, "counted": 0, "difference": 0, "expected_total": 0,
        })
        available = int(row.get("available") or 0)
        group["items"] += 1
        group["expected_total"] += available
        if row.get("verified"):
            group["verified"] += 1
        if row.get("counted") is not None:
            group["with_quantity"] += 1
            group["expected"] += available
            group["counted"] += int(row["counted"])
            group["difference"] += int(row["counted"]) - available
    order = sorted(groups, key=lambda key: (key == "", code_order(groups[key]["location"])))
    return [groups[key] for key in order]


def _parent_names(storage, report: dict) -> dict:
    names = {}
    for row in (report or {}).get("rows") or []:
        parent = str(row.get("parent_id") or "")
        if parent and parent not in names:
            try:
                names[parent] = (storage.get_item(parent) or {}).get("name", "")
            except Exception:  # un nombre que no se lee no impide cerrar
                names[parent] = ""
    return names


def apply_adjustments(storage, session: dict, actor: dict, rows: list):
    """Ajusta la cantidad total de los productos elegidos para que el estante
    quede como se conto: total = contadas + unidades hoy en prestamo.

    Solo el maestro. Devuelve (aplicados, errores); cada ajuste queda en el
    historial del producto ("Ajuste")."""
    if not can_adjust(actor):
        return [], ["Solo un administrador puede ajustar cantidades a partir de un conteo."]
    applied, errors = [], []
    session_name = (session or {}).get("name") or "conteo"
    for row in rows or []:
        code = str(row.get("code") or "")
        try:
            counted = parse_quantity(row.get("counted"))
        except ValueError as exc:
            errors.append(f"{code}: {exc}")
            continue
        if counted is None:
            errors.append(f"{code}: no tiene cantidad contada.")
            continue
        item = storage.get_item(code)
        if not item or item.get("status") == "retired":
            errors.append(f"{code}: el producto ya no existe.")
            continue
        current = int(item.get("quantity") or 0)
        on_loan = max(current - int(storage.get_available_quantity(code)), 0)
        new_total = counted + on_loan
        if new_total == current:
            continue
        detail = (f"Ajuste por conteo físico «{session_name}»: {counted} u. contadas en el estante"
                  + (f" + {on_loan} en préstamo" if on_loan else "") + f" (antes {current}).")
        try:
            storage.save_item({**item, "quantity": new_total}, code, is_new=False,
                              actor_email=(actor or {}).get("institutional_email", ""), details=detail,
                              actor=actor)
        except Exception as exc:  # validacion o E/S: se informa y se sigue con los demas
            errors.append(f"{code}: {exc}")
            continue
        applied.append({"code": code, "name": item.get("name") or code, "from": current, "to": new_total,
                        "counted": counted, "on_loan": on_loan})
    return applied, errors


def close_session(storage, session: dict, actor: dict, report: dict, adjusted: list = None,
                  notes: str = "", now=None):
    """Cierra el conteo guardando UN resumen. Devuelve (ok, mensaje, evento)."""
    if not traceability.is_manager(actor):
        return False, "Solo un supervisor autorizado o un administrador pueden cerrar un conteo.", None
    current = open_session(storage)
    if not session or not current or current["id"] != session.get("id"):
        return False, "Este conteo ya estaba cerrado.", None
    report = report or {}
    details = {
        "session": session["id"], "name": session.get("name", ""), "scope": session.get("scope") or {},
        "started_at": session.get("started_at", ""), "started_by": session.get("started_by", ""),
        "expected": report.get("total", 0), "verified": report.get("verified_count", 0),
        "counted": report.get("counted_count", 0), "nfc": report.get("nfc_count", 0),
        "missing_total": report.get("missing_count", 0),
        "missing": [row["code"] for row in report.get("missing") or []][:MAX_LIST],
        "differences": [
            {"code": row["code"], "name": row["name"], "available": row["available"], "counted": row["counted"],
             "on_loan": row["on_loan"]}
            for row in report.get("differences") or []
        ][:MAX_LIST],
        "extra": [row["code"] for row in report.get("extra") or []][:MAX_LIST],
        "adjusted": list(adjusted or [])[:MAX_LIST],
        "notes": " ".join(str(notes or "").split())[:500],
    }
    if blind_count_enabled(storage):
        # Conteo ciego: lo esperado no se mostró mientras se contaba; se congela aquí, al cerrar.
        details["blind"] = True
        details["by_location"] = summary_by_location(report, _parent_names(storage, report))[:MAX_LIST]
        details["snapshot"] = [
            [row["code"], row.get("available"), row.get("counted")]
            for row in report.get("rows") or [] if row.get("counted") is not None
        ][:MAX_LIST]
    event = storage.add_trace_event(_event(traceability.EVENT_COUNT_CLOSED, actor, details, now=now))
    message = (f"Conteo «{details['name']}» cerrado: {details['verified']} de {details['expected']} productos "
               f"verificados, {details['missing_total']} sin verificar")
    if details["adjusted"]:
        message += f", {len(details['adjusted'])} cantidad(es) ajustada(s)"
    return True, message + ".", event


def closed_sessions(storage, limit: int = 20) -> list:
    """Conteos cerrados (el mas reciente primero) con su resumen."""
    rows = []
    for event in reversed(storage.get_trace_events(event_type=traceability.EVENT_COUNT_CLOSED)):
        info = traceability.event_details(event)
        rows.append({**info, "closed_at": event.get("created_at", ""), "closed_by": event.get("actor_name", "")})
        if len(rows) >= limit:
            break
    return rows


# ---------------------------------------------------------------------------
# M6: continuidad y control del conteo por ubicación (opción conteo_por_ubicacion)
# ---------------------------------------------------------------------------
#
# Todo queda en `trace_events` (nada en la memoria de la sesión), así que una recarga o una
# conexión perdida retoman el conteo tal como estaba:
# - count_location_done: quien cuenta da por terminada una ubicación (contenedor o ubicación escrita).
# - count_recount_authorized: un supervisor autoriza, con motivo, volver a contar una ubicación terminada.
# Una ubicación terminada no acepta marcas ni toques del conteo hasta que se autorice el reconteo.

STATUS_PENDING, STATUS_IN_PROGRESS, STATUS_DONE, STATUS_RECOUNT = "pendiente", "en conteo", "contada", "reconteo"


def by_location_enabled(storage) -> bool:
    from core import options

    return options.enabled(storage, options.COUNT_BY_LOCATION)


def _session_events(storage, session_id: str, kind: str) -> list:
    return [event for event in storage.get_trace_events(event_type=kind)
            if traceability.event_details(event).get("session") == session_id]


def location_states(storage, session_id: str) -> dict:
    """``{clave: {"done", "done_by", "done_at", "recounts", "authorized_by", "reason"}}`` en orden de hora."""
    events = (_session_events(storage, session_id, traceability.EVENT_COUNT_LOCATION_DONE)
              + _session_events(storage, session_id, traceability.EVENT_COUNT_RECOUNT_AUTHORIZED))
    aware_min = datetime.min.replace(tzinfo=timezone.utc)
    states = {}
    for event in sorted(events, key=lambda row: traceability.to_datetime(row.get("created_at")) or aware_min):
        info = traceability.event_details(event)
        state = states.setdefault(str(info.get("location") or ""), {
            "done": False, "done_by": "", "done_at": "", "recounts": 0, "authorized_by": "", "reason": ""})
        if event.get("event_type") == traceability.EVENT_COUNT_LOCATION_DONE:
            state.update(done=True, done_by=event.get("actor_name", ""), done_at=event.get("created_at", ""))
        else:
            state.update(done=False, authorized_by=event.get("actor_name", ""), reason=info.get("reason", ""))
            state["recounts"] += 1
    return states


def location_progress(session: dict, items: list, marks: list, states: dict) -> dict:
    """Ubicaciones del alcance del conteo: contadas, en conteo y pendientes."""
    scope = (session or {}).get("scope") or {"kind": SCOPE_ALL}
    names = {str(item.get("id")): item.get("name") or "" for item in items or []}
    verified = {mark["code"] for mark in marks or []}
    groups = {}
    for item in expected_items(items, scope):
        key, label = place_of(item, names)
        group = groups.setdefault(key, {"key": key, "location": label, "items": 0, "verified": 0})
        group["items"] += 1
        group["verified"] += 1 if str(item.get("id")) in verified else 0
    rows = []
    for key in sorted(groups, key=lambda value: (value == "", code_order(groups[value]["location"]))):
        group, state = groups[key], (states or {}).get(key) or {}
        if state.get("done"):
            status = STATUS_DONE
        elif state.get("recounts"):
            status = STATUS_RECOUNT
        else:
            status = STATUS_IN_PROGRESS if group["verified"] else STATUS_PENDING
        rows.append({**group, "status": status, "done_by": state.get("done_by", ""),
                     "done_at": state.get("done_at", ""), "recounts": state.get("recounts", 0)})
    done = len([row for row in rows if row["status"] == STATUS_DONE])
    return {"rows": rows, "total": len(rows), "done": done, "pending": len(rows) - done,
            "ratio": (done / len(rows)) if rows else 0.0}


def item_place(storage, item: dict) -> tuple:
    parent = str((item or {}).get("parent_id") or "")
    name = ""
    if parent:
        try:
            name = (storage.get_item(parent) or {}).get("name", "")
        except Exception:
            name = ""
    return place_of(item, {parent: name} if parent else None)


def location_blocked(storage, session: dict, item: dict) -> str:
    """Mensaje si ``item`` está en una ubicación ya contada de ``session`` (sin reconteo autorizado); '' si se
    puede contar o si la opción está apagada."""
    if not session or not item or not by_location_enabled(storage):
        return ""
    key, label = item_place(storage, item)
    if (location_states(storage, session["id"]).get(key) or {}).get("done"):
        return (f"«{label}» ya se contó en este conteo. Para volver a contarla, un supervisor debe autorizar el "
                "reconteo.")
    return ""


def finish_location(storage, session: dict, actor: dict, key: str, label: str = "", now=None):
    """Da por contada una ubicación del conteo abierto. Devuelve (ok, mensaje, evento)."""
    if not by_location_enabled(storage):
        return False, "El conteo por ubicación está apagado.", None
    if not traceability.is_manager(actor):
        return False, "Solo un supervisor autorizado o un administrador pueden contar.", None
    current = open_session(storage)
    if not session or not current or current["id"] != session.get("id"):
        return False, "Este conteo ya estaba cerrado.", None
    if (location_states(storage, session["id"]).get(key or "") or {}).get("done"):
        return False, f"«{label or key}» ya está contada en este conteo.", None
    details = {"session": session["id"], "location": key or "", "label": label or NO_PLACE}
    event = storage.add_trace_event(_event(traceability.EVENT_COUNT_LOCATION_DONE, actor, details, now=now))
    return True, f"«{label or NO_PLACE}» quedó contada.", event


def authorize_recount(storage, session: dict, actor: dict, key: str, label: str = "", reason: str = "", now=None):
    """Un supervisor autoriza volver a contar una ubicación terminada (con motivo). Devuelve (ok, mensaje, evento)."""
    if not by_location_enabled(storage):
        return False, "El conteo por ubicación está apagado.", None
    if not permissions.has_cap(actor, permissions.CAP_APPROVE):
        return False, "Solo un supervisor puede autorizar un reconteo.", None
    reason = " ".join(str(reason or "").split())[:300]
    if not reason:
        return False, "Escribe el motivo del reconteo.", None
    if not session or not (location_states(storage, session["id"]).get(key or "") or {}).get("done"):
        return False, "Esa ubicación no está contada: se puede seguir contando sin autorización.", None
    details = {"session": session["id"], "location": key or "", "label": label or NO_PLACE, "reason": reason}
    event = storage.add_trace_event(_event(traceability.EVENT_COUNT_RECOUNT_AUTHORIZED, actor, details, now=now))
    return True, f"Reconteo de «{label or NO_PLACE}» autorizado.", event


def resume_point(storage, session: dict, actor: dict):
    """Lo último que ``actor`` verificó en ``session`` (``{"code", "method", "at"}``), para retomar tras recargar;
    ``None`` si todavía no verificó nada."""
    if not session or not actor:
        return None
    mine = [mark for mark in session_marks(storage, session["id"]) if mark.get("by_id") == actor.get("id")]
    if not mine:
        return None
    last = mine[-1]
    return {"code": last["code"], "method": last["method"], "at": last["at"]}
