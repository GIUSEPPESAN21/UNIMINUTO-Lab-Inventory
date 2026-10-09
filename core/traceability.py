# -*- coding: utf-8 -*-
"""core/traceability.py - Ruta verificable hasta un producto y trazabilidad
chequeable de solicitudes, retiros y servicios.

Reglas de diseño:

- La ruta se recorre en la sesión del navegador, sin escribir en la base por
  cada paso (cada escritura es un commit en GitHub). Al completarla se guarda
  UN evento `route_verified` con cada punto de control (código, método y hora)
  y un comprobante corto que el profesor puede validar.
- Los estados existentes de solicitudes (pending/approved/rejected/cancelled) y
  de préstamos (out/returned) NO cambian. El avance adicional (retirado,
  servicio en curso/entregado, comprobante validado) se registra como eventos
  en la hoja `trace_events`; "devuelta" se deriva del préstamo enlazado.
- Lo que muestra datos de terceros pasa por `visible_requests`/`visible_events`:
  un estudiante solo ve lo suyo; profesor y maestro ven todo.

Este módulo no importa Streamlit: es lógica pura y se prueba con FakeStorage.
"""

import hashlib
import json
import re
from datetime import datetime, timezone

from core import location, permissions
from core.reservations import as_bogota
from core.service_requests import (
    STATUS_APPROVED, STATUS_CANCELLED, STATUS_PENDING, STATUS_REJECTED, TYPE_PRODUCT, TYPE_SERVICE,
)

EVENT_ROUTE_VERIFIED = "route_verified"
EVENT_PICKED_UP = "picked_up"
EVENT_RECEIPT_CHECKED = "receipt_checked"
EVENT_SERVICE_STARTED = "service_started"
EVENT_SERVICE_DELIVERED = "service_delivered"
EVENT_TYPES = (
    EVENT_ROUTE_VERIFIED, EVENT_PICKED_UP, EVENT_RECEIPT_CHECKED,
    EVENT_SERVICE_STARTED, EVENT_SERVICE_DELIVERED,
)
EVENT_LABELS = {
    EVENT_ROUTE_VERIFIED: "Ruta verificada",
    EVENT_PICKED_UP: "Retirado",
    EVENT_RECEIPT_CHECKED: "Comprobante validado",
    EVENT_SERVICE_STARTED: "Servicio en curso",
    EVENT_SERVICE_DELIVERED: "Servicio entregado",
}
SERVICE_STAGES = (EVENT_SERVICE_STARTED, EVENT_SERVICE_DELIVERED)

# Cómo quedó confirmado cada punto de control de la ruta.
METHOD_SCAN = "scan"            # se escaneó o escribió el código de su etiqueta
METHOD_ARRIVAL = "arrival"      # punto sin etiqueta: el estudiante confirmó que llegó
METHOD_IMPLIED = "implied"      # punto sin etiqueta probado por una etiqueta más adentro
METHOD_INFERRED = "inferred"    # punto CON etiqueta que se saltó (se escaneó una más adentro)
METHOD_LABELS = {
    METHOD_SCAN: "Etiqueta escaneada",
    METHOD_ARRIVAL: "Llegada confirmada (sin etiqueta)",
    METHOD_IMPLIED: "Confirmado por una etiqueta más adentro",
    METHOD_INFERRED: "Etiqueta sin escanear (inferida)",
}

# Comprobante: 6 caracteres del alfabeto de Crockford (sin I, L, O ni U para que
# no se confundan al dictarlo o escribirlo).
RECEIPT_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
RECEIPT_LENGTH = 6
_RECEIPT_VERSION = "lab-trace-v1"


# ---------------------------------------------------------------------------
# Fechas
# ---------------------------------------------------------------------------

def to_datetime(value):
    """datetime aware (UTC) desde datetime o texto ISO; None si no se puede."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _now(now=None) -> datetime:
    return to_datetime(now) or datetime.now(timezone.utc)


def _iso(value) -> str:
    """ISO al segundo: horas de la ruta y base del comprobante."""
    dt = to_datetime(value)
    return dt.isoformat(timespec="seconds") if dt else ""


def _stamp(now=None) -> str:
    """ISO completo para `created_at` de eventos: ordena bien frente a los
    movimientos de inventario registrados en el mismo segundo."""
    return _now(now).isoformat()


def fmt_local(value, time_only: bool = False) -> str:
    """Fecha/hora de Bogotá legible (09/10/2026 10:32 a. m.); "" si no hay fecha."""
    dt = to_datetime(value)
    if not dt:
        return ""
    local = as_bogota(dt)
    hour = local.strftime("%I:%M:%S" if time_only else "%I:%M").lstrip("0") or "0"
    suffix = "a. m." if local.hour < 12 else "p. m."
    return f"{hour} {suffix}" if time_only else f"{local.strftime('%d/%m/%Y')} {hour} {suffix}"


def fmt_duration(seconds) -> str:
    if seconds is None:
        return ""
    seconds = max(int(seconds), 0)
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min {secs} s"
    return f"{secs} s"


def short_id(request_id) -> str:
    return f"#{str(request_id or '')[:6]}" if request_id else "#—"


# ---------------------------------------------------------------------------
# Ruta verificable (estado en la sesión; se persiste una sola vez)
# ---------------------------------------------------------------------------

def new_route(item: dict, parent: dict = None, request: dict = None, names: dict = None) -> dict:
    """Estado inicial (serializable) de la ruta hacia `item`."""
    item, parent = item or {}, parent or {}
    checkpoints = location.build_route_checkpoints(item, parent, names)
    for point in checkpoints:
        point.update(done=False, method="", code="", at="")
    return {
        "request_id": str((request or {}).get("id") or ""),
        "item_id": str(item.get("id") or ""),
        "item_name": str(item.get("name") or ""),
        "anchor": location.route_anchor(item, parent),
        "location": str(item.get("location") or parent.get("location") or ""),
        "checkpoints": checkpoints,
        "started_at": "",
        "completed_at": "",
        "attempts": 0,  # lecturas equivocadas: ayudan a detectar etiquetas confusas
    }


def current_index(route: dict):
    """Índice del primer punto sin confirmar; None si la ruta está completa."""
    for index, point in enumerate(route.get("checkpoints") or []):
        if not point.get("done"):
            return index
    return None


def is_complete(route: dict) -> bool:
    return bool(route and route.get("checkpoints")) and current_index(route) is None


def route_stats(route: dict) -> dict:
    points = route.get("checkpoints") or []
    labels = [p for p in points if p.get("label_code")]
    scanned = [p for p in labels if p.get("method") == METHOD_SCAN]
    done = [p for p in points if p.get("done")]
    complete = bool(points) and len(done) == len(points)
    start, end = to_datetime(route.get("started_at")), to_datetime(route.get("completed_at"))
    return {
        "total": len(points),
        "done": len(done),
        "labels": len(labels),
        "scanned": len(scanned),
        "inferred": len([p for p in labels if p.get("method") == METHOD_INFERRED]),
        "progress": (len(done) / len(points)) if points else 0.0,
        "complete": complete,
        "full": complete and len(scanned) == len(labels),
        "duration_s": int((end - start).total_seconds()) if start and end else None,
        "attempts": int(route.get("attempts") or 0),
    }


def _result(ok: bool, tone: str, message: str, completed: bool = False) -> dict:
    return {"ok": ok, "tone": tone, "message": message, "completed": completed}


def _mark(point: dict, method: str, now: datetime, code: str = "") -> None:
    point.update(done=True, method=method, code=code, at=_iso(now))


def _after_progress(route: dict, message: str, now: datetime) -> dict:
    index = current_index(route)
    if index is None:
        route["completed_at"] = route.get("completed_at") or _iso(now)
        return _result(True, "success", f"{message} ¡Llegaste a tu producto!", completed=True)
    return _result(True, "success", f"{message} Siguiente: {route['checkpoints'][index]['hint']}")


def confirm_arrival(route: dict, now=None) -> dict:
    """Confirma un punto SIN etiqueta (estantería, piso, mesa, zona)."""
    index = current_index(route)
    if index is None:
        return _result(True, "info", "La ruta ya está completa.", completed=True)
    point = route["checkpoints"][index]
    if point.get("label_code"):
        return _result(False, "warning", f"{point['title']} tiene etiqueta: escanea o escribe su código.")
    now = _now(now)
    route["started_at"] = route.get("started_at") or _iso(now)
    _mark(point, METHOD_ARRIVAL, now)
    return _after_progress(route, f"📍 Llegaste a {point['title']}.", now)


def apply_code(route: dict, code: str, now=None, known_item: dict = None) -> dict:
    """Aplica un código escaneado/escrito a la ruta.

    - Si es la etiqueta del punto actual, lo confirma.
    - Si es una etiqueta más adentro de la ruta, la acepta: los puntos sin
      etiqueta intermedios quedan probados y las etiquetas saltadas quedan
      "inferidas" (se pueden escanear después para una verificación completa).
    - Si no pertenece a la ruta, explica dónde está quien escaneó y hacia
      dónde ir, y cuenta el intento fallido.
    """
    code = (code or "").strip()
    if not code:
        return _result(False, "warning", "Escribe o escanea el código de la etiqueta.")
    points = route.get("checkpoints") or []
    index = current_index(route)
    now = _now(now)
    match = next(
        (i for i, p in enumerate(points) if p.get("label_code") and location.codes_match(p["label_code"], code)),
        None,
    )

    if match is not None:
        point = points[match]
        if index is None or match < index:
            if point.get("method") == METHOD_INFERRED:
                _mark(point, METHOD_SCAN, now, code)
                message = f"🏷️ Etiqueta de {point['title']} verificada: ya no queda inferida."
                if index is None:
                    return _result(True, "success", message, completed=True)
                return _result(True, "success", f"{message} Siguiente: {points[index]['hint']}")
            if index is None:
                return _result(True, "info", "La ruta ya está completa.", completed=True)
            return _result(True, "info", f"Ya confirmaste {point['title']}. Ahora: {points[index]['hint']}")

        route["started_at"] = route.get("started_at") or _iso(now)
        skipped = 0
        for previous in points[index:match]:
            if previous.get("label_code"):
                _mark(previous, METHOD_INFERRED, now)
                skipped += 1
            else:
                _mark(previous, METHOD_IMPLIED, now)
        _mark(point, METHOD_SCAN, now, code)
        message = f"✅ {point['title']} verificado."
        if skipped == 1:
            message += " Saltaste 1 etiqueta: queda inferida (escanéala si quieres una verificación completa)."
        elif skipped:
            message += (f" Saltaste {skipped} etiquetas: quedan inferidas (escanéalas si quieres una "
                        "verificación completa).")
        return _after_progress(route, message, now)

    if index is None:
        return _result(False, "info", "Ese código no hace parte de tu ruta, que ya está completa.", completed=True)
    route["attempts"] = int(route.get("attempts") or 0) + 1
    hint = location.explain_mismatch(
        route.get("anchor", ""), code, known_item, route.get("location", ""), route.get("item_id", ""),
    )
    return _result(False, "warning", f"🔁 {hint}")


def breadcrumb(route: dict) -> list:
    """[(texto corto, estado)] para pintar la ruta compacta como pastillas."""
    index = current_index(route)
    crumbs = []
    for position, point in enumerate(route.get("checkpoints") or []):
        if point.get("done"):
            state = "inferred" if point.get("method") == METHOD_INFERRED else "done"
        else:
            state = "current" if position == index else "pending"
        crumbs.append((point.get("short") or point.get("title"), state))
    return crumbs


def checkpoint_steps(checkpoints: list, current=None) -> list:
    """Pasos para `core.ui.timeline` a partir de puntos de control (de la sesión o
    de un evento guardado)."""
    steps = []
    for position, point in enumerate(checkpoints or []):
        title = point.get("title") or "Punto"
        if point.get("name") and point.get("name") != title:
            title = f"{title} · {point['name']}"
        label = point.get("label_code")
        if point.get("done") or point.get("method"):
            method = point.get("method")
            detail = METHOD_LABELS.get(method, "Confirmado")
            if point.get("code"):
                detail += f": {point['code']}"
            elif method == METHOD_INFERRED and label:
                detail += f" ({label})"
            step = {"title": title, "detail": detail, "time": fmt_local(point.get("at"), time_only=True),
                    "state": "done"}
            if method == METHOD_INFERRED:
                step["icon"] = "!"
        elif position == current:
            extra = f" Etiqueta: {label}." if label else " Sin etiqueta: confírmalo al llegar."
            step = {"title": title, "detail": f"{point.get('hint', '')}{extra}", "state": "current"}
        else:
            detail = f"🏷️ Etiqueta {label}" if label else "Sin etiqueta"
            step = {"title": title, "detail": detail, "state": "pending"}
        steps.append(step)
    return steps


def route_timeline(route: dict) -> list:
    return checkpoint_steps(route.get("checkpoints"), current_index(route))


# ---------------------------------------------------------------------------
# Comprobante verificable
# ---------------------------------------------------------------------------

def make_receipt(request_id, item_id, user_id, completed_at) -> str:
    """Código corto y determinista derivado de la solicitud, el producto, el
    estudiante y la hora en que terminó la ruta. Sirve para encontrar el
    registro y detectar si sus datos se alteraron (no es una firma secreta)."""
    payload = "|".join(str(part or "") for part in (_RECEIPT_VERSION, request_id, item_id, user_id, _iso(completed_at)))
    number = int.from_bytes(hashlib.sha256(payload.encode("utf-8")).digest()[:8], "big")
    chars = []
    for _ in range(RECEIPT_LENGTH):
        number, remainder = divmod(number, len(RECEIPT_ALPHABET))
        chars.append(RECEIPT_ALPHABET[remainder])
    return "".join(chars)


def normalize_receipt(text) -> str:
    """Acepta el comprobante como lo escriba la persona: minúsculas, guion,
    espacios y las letras que se confunden con números (O→0, I/L→1)."""
    raw = re.sub(r"[\s\-_.#]", "", str(text or "").upper())
    return raw.translate(str.maketrans({"O": "0", "I": "1", "L": "1", "U": "V"}))


def format_receipt(code) -> str:
    code = normalize_receipt(code)
    return f"{code[:3]}-{code[3:]}" if len(code) == RECEIPT_LENGTH else code


def is_receipt_like(text) -> bool:
    code = normalize_receipt(text)
    return len(code) == RECEIPT_LENGTH and all(ch in RECEIPT_ALPHABET for ch in code)


# ---------------------------------------------------------------------------
# Eventos: lectura y permisos
# ---------------------------------------------------------------------------

def is_manager(user: dict) -> bool:
    return permissions.has_role(user, permissions.MANAGER_ROLES)


def can_view_request(user: dict, request: dict) -> bool:
    return bool(user and request) and (is_manager(user) or request.get("requester_id") == user.get("id"))


def visible_requests(storage, user: dict, status: str = None) -> list:
    """Solicitudes que `user` puede ver: todas para profesor/maestro, las propias
    para cualquier otro rol."""
    if not user:
        return []
    if is_manager(user):
        return storage.get_service_requests(status=status)
    return storage.get_service_requests(status=status, user_id=user.get("id"))


def visible_events(storage, user: dict, **filters) -> list:
    """Eventos de trazabilidad que `user` puede ver (mismo criterio)."""
    if not user:
        return []
    if not is_manager(user):
        filters["user_id"] = user.get("id")
    return storage.get_trace_events(**filters)


def event_details(event: dict) -> dict:
    raw = (event or {}).get("details")
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def events_by_request(events: list) -> dict:
    index = {}
    for event in events or []:
        index.setdefault(event.get("request_id") or "", []).append(event)
    for rows in index.values():
        rows.sort(key=lambda e: to_datetime(e.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc))
    return index


def first_event(events: list, event_type: str):
    return next((e for e in events or [] if e.get("event_type") == event_type), None)


def route_event_for(storage, request_id: str):
    if not request_id:
        return None
    return first_event(storage.get_trace_events(request_id=request_id, event_type=EVENT_ROUTE_VERIFIED),
                       EVENT_ROUTE_VERIFIED)


def _actor_fields(actor: dict) -> dict:
    actor = actor or {}
    return {
        "actor_id": actor.get("id", ""), "actor_name": actor.get("full_name", ""),
        "actor_email": actor.get("institutional_email", ""),
    }


# ---------------------------------------------------------------------------
# Eventos: escritura (una por acción real, nunca por paso de la ruta)
# ---------------------------------------------------------------------------

def save_route(storage, request: dict, route: dict, actor: dict, now=None):
    """Guarda la ruta completada como UN evento y devuelve (ok, mensaje, evento)."""
    if not request:
        return False, "La solicitud no existe.", None
    if not actor or request.get("requester_id") != actor.get("id"):
        return False, "Solo quien hizo la solicitud puede registrar su ruta.", None
    if request.get("request_type") != TYPE_PRODUCT:
        return False, "La ruta verificable aplica a solicitudes de productos.", None
    if request.get("status") != STATUS_APPROVED:
        return False, "La ruta se registra cuando la solicitud está aprobada.", None
    if route.get("request_id") != request.get("id") or route.get("item_id") != request.get("item_id"):
        return False, "Esta ruta no corresponde a la solicitud.", None
    if not is_complete(route):
        return False, "Completa la ruta: falta confirmar uno o más puntos.", None
    existing = route_event_for(storage, request["id"])
    if existing:
        return True, "La ruta de esta solicitud ya estaba registrada.", existing

    completed_at = route.get("completed_at") or _iso(_now(now))
    stats = route_stats(route)
    details = {
        "version": 1,
        "item_name": route.get("item_name") or request.get("item_name", ""),
        "route": location.compact_route(route["checkpoints"]),
        "quantity": request.get("quantity", 0),
        "started_at": route.get("started_at") or completed_at,
        "completed_at": completed_at,
        "labels": stats["labels"], "scanned": stats["scanned"], "attempts": stats["attempts"],
        "duration_s": stats["duration_s"],
        "checkpoints": [
            {key: point.get(key, "") for key in ("key", "title", "name", "label_code", "method", "code", "at")}
            for point in route["checkpoints"]
        ],
    }
    receipt = make_receipt(request["id"], request.get("item_id"), request.get("requester_id"), completed_at)
    event = storage.add_trace_event({
        "event_type": EVENT_ROUTE_VERIFIED, "request_id": request["id"],
        "item_id": request.get("item_id", ""), "loan_id": "", "user_id": request.get("requester_id", ""),
        **_actor_fields(actor), "receipt": receipt,
        "details": json.dumps(details, ensure_ascii=False), "created_at": completed_at,
    })
    return True, f"Ruta verificada. Tu comprobante es {format_receipt(receipt)}.", event


def pending_pickups(storage, user: dict, item_id: str) -> list:
    """Solicitudes aprobadas de `user` para `item_id` que todavía no se retiraron
    (la más antigua primero)."""
    if not user or not item_id:
        return []
    rows = [
        r for r in storage.get_service_requests(status=STATUS_APPROVED, user_id=user.get("id"))
        if r.get("request_type") == TYPE_PRODUCT and r.get("item_id") == item_id
    ]
    if not rows:
        return []
    picked = {e.get("request_id") for e in storage.get_trace_events(user_id=user.get("id"), event_type=EVENT_PICKED_UP)}
    aware_min = datetime.min.replace(tzinfo=timezone.utc)
    rows = [r for r in rows if r["id"] not in picked]
    return sorted(rows, key=lambda r: to_datetime(r.get("created_at")) or aware_min)


def record_pickup(storage, request: dict, loan: dict, actor: dict, now=None):
    """Enlaza el préstamo registrado en Escanear con la solicitud aprobada (evento
    "retirado"). No toca el estado de la solicitud ni del préstamo."""
    if not request or not loan:
        return False, "Falta la solicitud o el préstamo.", None
    if request.get("request_type") != TYPE_PRODUCT or request.get("status") != STATUS_APPROVED:
        return False, "Solo se enlazan solicitudes de producto aprobadas.", None
    if loan.get("item_id") != request.get("item_id"):
        return False, "El préstamo es de otro producto.", None
    if loan.get("user_id") != request.get("requester_id"):
        return False, "El préstamo no es de quien hizo la solicitud.", None
    if first_event(storage.get_trace_events(request_id=request["id"], event_type=EVENT_PICKED_UP), EVENT_PICKED_UP):
        return False, "Esta solicitud ya tiene un retiro registrado.", None
    route = route_event_for(storage, request["id"])
    details = {
        "quantity": loan.get("quantity", 0), "requested_quantity": request.get("quantity", 0),
        "route_receipt": (route or {}).get("receipt", ""), "route_verified": bool(route),
    }
    event = storage.add_trace_event({
        "event_type": EVENT_PICKED_UP, "request_id": request["id"], "item_id": request.get("item_id", ""),
        "loan_id": loan.get("id", ""), "user_id": request.get("requester_id", ""), **_actor_fields(actor),
        "receipt": "", "details": json.dumps(details, ensure_ascii=False), "created_at": _stamp(now),
    })
    return True, f"Salida vinculada a la solicitud {short_id(request['id'])}.", event


def service_stage(events: list) -> str:
    """approved | started | delivered según los eventos del servicio."""
    types = {e.get("event_type") for e in events or []}
    if EVENT_SERVICE_DELIVERED in types:
        return "delivered"
    if EVENT_SERVICE_STARTED in types:
        return "started"
    return "approved"


def mark_service_stage(storage, request_id: str, stage: str, actor: dict, notes: str = "", now=None):
    """Registra "en curso" o "entregado" para un servicio aprobado."""
    if not is_manager(actor):
        return False, "Solo profesor o maestro registran el avance de un servicio.", None
    if stage not in SERVICE_STAGES:
        return False, "Etapa de servicio no válida.", None
    request = storage.get_service_request(request_id)
    if not request:
        return False, "La solicitud no existe.", None
    if request.get("request_type") != TYPE_SERVICE:
        return False, "Solo las solicitudes de servicio tienen etapas de avance.", None
    if request.get("status") != STATUS_APPROVED:
        return False, "El servicio debe estar aprobado.", None
    current = service_stage(storage.get_trace_events(request_id=request_id))
    if current == "delivered":
        return False, "El servicio ya fue entregado.", None
    if stage == EVENT_SERVICE_STARTED and current == "started":
        return False, "El servicio ya está en curso.", None
    notes = " ".join((notes or "").split())
    event = storage.add_trace_event({
        "event_type": stage, "request_id": request_id, "item_id": "", "loan_id": "",
        "user_id": request.get("requester_id", ""), **_actor_fields(actor), "receipt": "",
        "details": json.dumps({"notes": notes}, ensure_ascii=False), "created_at": _stamp(now),
    })
    label = "en curso" if stage == EVENT_SERVICE_STARTED else "entregado"
    return True, f"Servicio {short_id(request_id)} marcado como {label}.", event


def verify_receipt(storage, code) -> dict:
    """Valida un comprobante: existe, sus datos coinciden con el registro y
    a qué solicitud/estudiante/producto corresponde."""
    normalized = normalize_receipt(code)
    if not is_receipt_like(normalized):
        return {"valid": False, "tone": "warning", "receipt": normalized,
                "message": "El comprobante tiene 6 caracteres (letras y números), por ejemplo K7Q-2MX."}
    matches = storage.get_trace_events(event_type=EVENT_ROUTE_VERIFIED, receipt=normalized)
    if not matches:
        return {"valid": False, "tone": "danger", "receipt": normalized,
                "message": f"No existe ninguna ruta verificada con el comprobante {format_receipt(normalized)}."}
    event = matches[0]
    details = event_details(event)
    expected = make_receipt(event.get("request_id"), event.get("item_id"), event.get("user_id"),
                            details.get("completed_at") or event.get("created_at"))
    if expected != normalized:
        return {"valid": False, "tone": "danger", "receipt": normalized, "tampered": True, "event": event,
                "message": "El comprobante existe, pero sus datos no coinciden con el registro: pudo ser "
                           "alterado. Revisa la solicitud manualmente."}
    request = storage.get_service_request(event.get("request_id"))
    related = storage.get_trace_events(request_id=event.get("request_id"))
    pickup = first_event(related, EVENT_PICKED_UP)
    checks = [e for e in related if e.get("event_type") == EVENT_RECEIPT_CHECKED]
    warnings = []
    if not request:
        warnings.append("La solicitud asociada ya no existe.")
    elif request.get("status") != STATUS_APPROVED:
        warnings.append(f"La solicitud está {STATUS_TEXT.get(request.get('status'), request.get('status'))}.")
    if pickup:
        warnings.append(f"El producto ya se retiró el {fmt_local(pickup.get('created_at'))}")
    who = (request or {}).get("requester_name") or event.get("actor_name") or "El estudiante"
    item_name = details.get("item_name") or (request or {}).get("item_name") or event.get("item_id")
    return {
        "valid": True, "tone": "warning" if warnings else "success", "receipt": normalized,
        "event": event, "details": details, "request": request, "pickup": pickup, "checks": checks,
        "warnings": warnings,
        "message": f"Comprobante válido: {who} verificó la ruta a «{item_name}» el "
                   f"{fmt_local(details.get('completed_at') or event.get('created_at'))}",
    }


def record_receipt_check(storage, verification: dict, actor: dict, now=None):
    """Deja constancia de que un profesor/maestro validó el comprobante."""
    if not is_manager(actor):
        return False, "Solo profesor o maestro validan comprobantes.", None
    if not verification or not verification.get("valid"):
        return False, "Primero valida un comprobante correcto.", None
    event = verification["event"]
    for check in storage.get_trace_events(request_id=event.get("request_id"), event_type=EVENT_RECEIPT_CHECKED):
        if check.get("actor_id") == (actor or {}).get("id"):
            return False, "Ya registraste la validación de este comprobante.", check
    row = storage.add_trace_event({
        "event_type": EVENT_RECEIPT_CHECKED, "request_id": event.get("request_id", ""),
        "item_id": event.get("item_id", ""), "loan_id": "", "user_id": event.get("user_id", ""),
        **_actor_fields(actor), "receipt": "",
        "details": json.dumps({"receipt": verification.get("receipt", "")}, ensure_ascii=False),
        "created_at": _stamp(now),
    })
    return True, "Validación registrada en la trazabilidad.", row


# ---------------------------------------------------------------------------
# Lecturas para la interfaz
# ---------------------------------------------------------------------------

STATUS_TEXT = {
    STATUS_PENDING: "pendiente", STATUS_APPROVED: "aprobada",
    STATUS_REJECTED: "rechazada", STATUS_CANCELLED: "cancelada",
}


def request_title(request: dict) -> str:
    request = request or {}
    if request.get("request_type") == TYPE_PRODUCT:
        return request.get("item_name") or request.get("item_id") or "Producto"
    return request.get("service_name") or "Servicio"


def request_stage(request: dict, events: list = None, loan: dict = None) -> tuple:
    """(texto, tono) del punto en que va una solicitud, para una pastilla."""
    status = (request or {}).get("status")
    if status == STATUS_PENDING:
        return "En revisión", "warning"
    if status == STATUS_REJECTED:
        return "Rechazada", "danger"
    if status == STATUS_CANCELLED:
        return "Cancelada", "neutral"
    types = {e.get("event_type") for e in events or []}
    if request.get("request_type") == TYPE_SERVICE:
        stage = service_stage(events)
        return {"delivered": ("Entregado", "success"), "started": ("En curso", "info")}.get(
            stage, ("Aprobada", "info"))
    if loan and loan.get("status") == "returned":
        return "Devuelta", "success"
    if EVENT_PICKED_UP in types:
        return "Retirada · en préstamo", "info"
    if EVENT_ROUTE_VERIFIED in types:
        return "Ruta verificada · por retirar", "success"
    return "Aprobada · por retirar", "info"


def _who(email: str, names: dict = None) -> str:
    if not email:
        return ""
    return (names or {}).get(email) or email


def request_timeline(request: dict, events: list = None, loan: dict = None, names: dict = None,
                     now=None) -> list:
    """Línea de tiempo de una solicitud para `core.ui.timeline`.

    Producto: creada → revisada → ruta verificada → retirada → devuelta.
    Servicio: creada → revisada → en curso → entregado."""
    request, events = request or {}, events or []
    status = request.get("status")
    closed = status in (STATUS_REJECTED, STATUS_CANCELLED)
    steps = [{
        "title": "Solicitud creada", "state": "done", "time": fmt_local(request.get("created_at")),
        "detail": f"Por {request.get('requester_name') or 'el solicitante'} · {short_id(request.get('id'))}",
    }]

    reviewer = _who(request.get("reviewed_by"), names)
    notes = request.get("review_notes") or ""
    if status == STATUS_PENDING:
        steps.append({"title": "En revisión", "state": "current",
                      "detail": "Un profesor o el maestro la aprobará o rechazará."})
    elif status == STATUS_APPROVED:
        detail = f"Por {reviewer}" if reviewer else "Aprobada"
        steps.append({"title": "Aprobada", "state": "done", "time": fmt_local(request.get("reviewed_at")),
                      "detail": f"{detail} · {notes}" if notes else detail})
    else:
        title = "Rechazada" if status == STATUS_REJECTED else "Cancelada"
        detail = notes or "Sin observación"
        if reviewer:
            detail = f"{detail} · {reviewer}"
        steps.append({"title": title, "state": "blocked", "time": fmt_local(request.get("reviewed_at")),
                      "detail": detail})

    def closed_step(title):
        return {"title": title, "state": "pending", "detail": "No aplica: la solicitud está cerrada."}

    if request.get("request_type") == TYPE_SERVICE:
        started = first_event(events, EVENT_SERVICE_STARTED)
        delivered = first_event(events, EVENT_SERVICE_DELIVERED)
        if started or delivered:
            source = started or delivered
            note = event_details(source).get("notes") if started else ""
            detail = f"Por {source.get('actor_name') or 'el laboratorio'}" if started else "Se entregó sin registro de inicio."
            steps.append({"title": "En curso", "state": "done", "time": fmt_local(source.get("created_at")),
                          "detail": f"{detail} · {note}" if note else detail})
        elif closed or status == STATUS_PENDING:
            steps.append(closed_step("En curso") if closed else
                         {"title": "En curso", "state": "pending", "detail": "Tras la aprobación."})
        else:
            steps.append({"title": "En curso", "state": "current",
                          "detail": "El laboratorio programará y empezará el servicio."})
        if delivered:
            note = event_details(delivered).get("notes")
            detail = f"Por {delivered.get('actor_name') or 'el laboratorio'}"
            steps.append({"title": "Entregado", "state": "done", "time": fmt_local(delivered.get("created_at")),
                          "detail": f"{detail} · {note}" if note else detail})
        elif closed:
            steps.append(closed_step("Entregado"))
        else:
            steps.append({"title": "Entregado", "state": "current" if started else "pending",
                          "detail": "Te avisaremos aquí cuando esté listo." if started else "Cuando termine el servicio."})
        return steps

    route = first_event(events, EVENT_ROUTE_VERIFIED)
    pickup = first_event(events, EVENT_PICKED_UP)
    check = first_event(events, EVENT_RECEIPT_CHECKED)
    if route:
        info = event_details(route)
        detail = (f"{info.get('scanned', 0)}/{info.get('labels', 0)} etiquetas escaneadas · comprobante "
                  f"{format_receipt(route.get('receipt'))}")
        if check:
            detail += f" · validado por {check.get('actor_name') or 'el laboratorio'}"
        steps.append({"title": "Ruta verificada", "state": "done", "time": fmt_local(route.get("created_at")),
                      "detail": detail})
    elif pickup:
        steps.append({"title": "Ruta verificada", "state": "pending",
                      "detail": "No se registró: el retiro se hizo sin recorrer la ruta."})
    elif closed:
        steps.append(closed_step("Ruta verificada"))
    elif status == STATUS_APPROVED:
        steps.append({"title": "Ruta verificada", "state": "current",
                      "detail": "Recorre la ruta y escanea cada etiqueta para obtener tu comprobante."})
    else:
        steps.append({"title": "Ruta verificada", "state": "pending", "detail": "Disponible tras la aprobación."})

    if pickup:
        qty = event_details(pickup).get("quantity")
        detail = f"Salida de {qty} u. registrada en Escanear" if qty else "Salida registrada en Escanear"
        steps.append({"title": "Retirada", "state": "done", "time": fmt_local(pickup.get("created_at")),
                      "detail": f"{detail} · {pickup.get('actor_name') or ''}".rstrip(" ·")})
    elif closed:
        steps.append(closed_step("Retirada"))
    else:
        current = status == STATUS_APPROVED and bool(route)
        steps.append({"title": "Retirada", "state": "current" if current else "pending",
                      "detail": "Confirma la salida en Escanear con tu usuario."})

    if loan and loan.get("status") == "returned":
        steps.append({"title": "Devuelta", "state": "done", "time": fmt_local(loan.get("return_at")),
                      "detail": "Préstamo cerrado en el laboratorio."})
    elif loan and pickup:
        due = loan.get("expected_return_at")
        overdue = bool(due) and to_datetime(due) is not None and to_datetime(due) < _now(now)
        detail = "En préstamo"
        if due:
            detail += f" · devolver antes de {fmt_local(due)}" + (" · ⚠️ vencido" if overdue else "")
        steps.append({"title": "Devuelta", "state": "blocked" if overdue else "current", "detail": detail})
    elif closed and not pickup:
        steps.append(closed_step("Devuelta"))
    else:
        steps.append({"title": "Devuelta", "state": "pending", "detail": "Al reingresar el producto."})
    return steps


def search_requests(requests: list, query: str, events: list = None) -> list:
    """Filtra solicitudes por id (#a1b2c3 o prefijo), código o nombre del
    producto/servicio, nombre o correo del solicitante, o comprobante."""
    query = (query or "").strip()
    if not query:
        return list(requests or [])
    needle = query.lstrip("#").casefold()
    receipt_ids = set()
    if is_receipt_like(query):
        code = normalize_receipt(query)
        receipt_ids = {e.get("request_id") for e in events or [] if e.get("receipt") == code}
    results = []
    for row in requests or []:
        fields = (row.get("item_id"), row.get("item_name"), row.get("service_name"),
                  row.get("requester_name"), row.get("requester_email"))
        if (str(row.get("id") or "").casefold().startswith(needle)
                or any(needle in str(value or "").casefold() for value in fields)
                or row.get("id") in receipt_ids):
            results.append(row)
    return results


def _pickup_requests(index: dict) -> set:
    return {rid for rid, rows in index.items() if first_event(rows, EVENT_PICKED_UP)}


def student_summary(requests: list, events: list, open_loans: list) -> list:
    """Indicadores para `core.ui.stat_cards` de un estudiante."""
    index = events_by_request(events)
    picked = _pickup_requests(index)
    active = [r for r in requests if r.get("status") == STATUS_PENDING
              or (r.get("status") == STATUS_APPROVED and r.get("request_type") == TYPE_PRODUCT and r["id"] not in picked)
              or (r.get("status") == STATUS_APPROVED and r.get("request_type") == TYPE_SERVICE
                  and service_stage(index.get(r["id"])) != "delivered")]
    ready = [r for r in requests if r.get("status") == STATUS_APPROVED
             and r.get("request_type") == TYPE_PRODUCT and r["id"] not in picked]
    routes = [e for e in events if e.get("event_type") == EVENT_ROUTE_VERIFIED]
    return [
        {"label": "Solicitudes activas", "value": len(active), "icon": "📝"},
        {"label": "Listas para retirar", "value": len(ready), "icon": "🧭",
         "tone": "info" if ready else "neutral", "help": "Aprobadas y sin retirar"},
        {"label": "Rutas verificadas", "value": len(routes), "icon": "✅", "tone": "success" if routes else "neutral"},
        {"label": "En préstamo", "value": len(open_loans or []), "icon": "📦"},
    ]


def manager_summary(requests: list, events: list) -> list:
    """Indicadores para profesor/maestro, incluida la tasa de retiros con ruta."""
    index = events_by_request(events)
    picked = _pickup_requests(index)
    pending = [r for r in requests if r.get("status") == STATUS_PENDING]
    to_pick = [r for r in requests if r.get("status") == STATUS_APPROVED
               and r.get("request_type") == TYPE_PRODUCT and r["id"] not in picked]
    with_route = [rid for rid in picked if first_event(index.get(rid), EVENT_ROUTE_VERIFIED)]
    rate = f"{round(100 * len(with_route) / len(picked))} %" if picked else "—"
    services = [r for r in requests if r.get("status") == STATUS_APPROVED and r.get("request_type") == TYPE_SERVICE
                and service_stage(index.get(r["id"])) == "started"]
    return [
        {"label": "Por revisar", "value": len(pending), "icon": "🟡", "tone": "warning" if pending else "neutral"},
        {"label": "Aprobadas por retirar", "value": len(to_pick), "icon": "🧭"},
        {"label": "Retiros con ruta verificada", "value": rate, "icon": "✅", "tone": "success",
         "help": f"{len(with_route)} de {len(picked)} retiros"},
        {"label": "Servicios en curso", "value": len(services), "icon": "🛠️"},
    ]


_HISTORY_ICONS = {"Alta": "📦", "Salida": "📤", "Reingreso": "📥", "Baja": "🗑️"}
_EVENT_ICONS = {
    EVENT_ROUTE_VERIFIED: "🧭", EVENT_PICKED_UP: "🤝", EVENT_RECEIPT_CHECKED: "🔏",
    EVENT_SERVICE_STARTED: "🛠️", EVENT_SERVICE_DELIVERED: "📦",
}


def custody_timeline(history: list, requests: list, events: list, names: dict = None) -> list:
    """Cadena de custodia de un producto: movimientos de inventario, solicitudes
    y eventos de trazabilidad en orden cronológico."""
    entries = []
    for row in history or []:
        change = row.get("quantity_change")
        detail = row.get("details") or ""
        if change not in (None, "", "0", 0):
            detail = f"{detail} · cambio {change}".strip(" ·")
        actor = _who(row.get("actor_user_id"), names)
        entries.append((to_datetime(row.get("timestamp")), {
            "title": row.get("type") or "Movimiento", "icon": _HISTORY_ICONS.get(row.get("type"), "•"),
            "detail": f"{detail} · por {actor}" if actor else detail,
        }))
    for request in requests or []:
        entries.append((to_datetime(request.get("created_at")), {
            "title": f"Solicitud {short_id(request.get('id'))} creada", "icon": "📝",
            "detail": f"{request.get('requester_name') or 'Solicitante'} · {request.get('quantity') or 0} u.",
        }))
        if request.get("status") in (STATUS_APPROVED, STATUS_REJECTED, STATUS_CANCELLED) and request.get("reviewed_at"):
            reviewer = _who(request.get("reviewed_by"), names)
            entries.append((to_datetime(request.get("reviewed_at")), {
                "title": f"Solicitud {short_id(request.get('id'))} {STATUS_TEXT.get(request['status'])}",
                "icon": "✅" if request["status"] == STATUS_APPROVED else "⛔",
                "detail": f"Por {reviewer}" if reviewer else "",
            }))
    for event in events or []:
        kind = event.get("event_type")
        info = event_details(event)
        detail = event.get("actor_name") or ""
        if kind == EVENT_ROUTE_VERIFIED:
            detail = (f"{event.get('actor_name') or 'Estudiante'} · {info.get('scanned', 0)}/{info.get('labels', 0)} "
                      f"etiquetas · {fmt_duration(info.get('duration_s'))} · comprobante "
                      f"{format_receipt(event.get('receipt'))}")
        elif kind == EVENT_PICKED_UP:
            detail = (f"{event.get('actor_name') or ''} · {info.get('quantity', 0)} u. · solicitud "
                      f"{short_id(event.get('request_id'))}" + ("" if info.get("route_verified") else " · sin ruta"))
        entries.append((to_datetime(event.get("created_at")), {
            "title": EVENT_LABELS.get(kind, kind or "Evento"), "icon": _EVENT_ICONS.get(kind, "•"),
            "detail": detail.strip(" ·"),
        }))
    aware_min = datetime.min.replace(tzinfo=timezone.utc)
    entries.sort(key=lambda pair: pair[0] or aware_min)
    steps = []
    for when, step in entries:
        step.update(state="done", time=fmt_local(when))
        steps.append(step)
    return steps
