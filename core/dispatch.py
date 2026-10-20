# -*- coding: utf-8 -*-
"""Flujo de despacho de solicitudes de producto.

La interfaz llama estas funciones; la autorización y las transiciones vuelven a
validarse atómicamente en :meth:`LabStorage.transition_request`.
"""

import csv
import hashlib
import io
import json
from datetime import datetime, timezone

from core import permissions, traceability


def _details(event: dict) -> dict:
    return traceability.event_details(event)


def _latest(events: list, kind: str):
    return next((row for row in reversed(events or []) if row.get("event_type") == kind), None)


def authorized_pickers(events: list) -> list:
    """IDs autorizados; lista vacía significa todos los operarios activos."""
    event = _latest(events, traceability.EVENT_ORDER_AUTHORIZED)
    values = _details(event or {}).get("authorized_pickers", [])
    return [str(value) for value in values] if isinstance(values, list) else []


def assigned_picker(events: list) -> str:
    assigned = ""
    for event in events or []:
        kind = event.get("event_type")
        if kind in {traceability.EVENT_ORDER_CLAIMED, traceability.EVENT_ORDER_REASSIGNED}:
            assigned = str(_details(event).get("picker_id") or event.get("actor_id") or "")
        elif kind == traceability.EVENT_ORDER_RELEASED:
            assigned = ""
    return assigned


def assigned_picker_name(events: list) -> str:
    name = ""
    for event in events or []:
        kind = event.get("event_type")
        if kind in {traceability.EVENT_ORDER_CLAIMED, traceability.EVENT_ORDER_REASSIGNED}:
            name = str(_details(event).get("picker_name") or event.get("actor_name") or "")
        elif kind == traceability.EVENT_ORDER_RELEASED:
            name = ""
    return name


def is_authorized(events: list) -> bool:
    return _latest(events, traceability.EVENT_ORDER_AUTHORIZED) is not None


def _picker_list(values) -> list:
    if not isinstance(values, (list, tuple, set)):
        return []
    return sorted({str(value).strip() for value in values if str(value or "").strip()})


def _clean(text) -> str:
    return " ".join(str(text or "").split())


def plan_transition(request: dict, events: list, actor: dict, event_type: str,
                    details: dict = None, available=None, open_loan: dict = None) -> dict:
    """Valida una transición SIN efectos y describe lo que debe escribirse.

    El almacenamiento la invoca dentro de su sección crítica con datos leídos
    bajo el mismo bloqueo y aplica el plan en una sola escritura. Claves:
    ``ok``, ``message``, ``status`` (nuevo estado de la solicitud o None),
    ``loan_action`` (None, "create", "reuse" o "close"), ``loan_id``,
    ``details`` y ``extra_events``. Si falla: ``failure_kind`` y
    ``failure_details`` para el evento de auditoría.
    """
    request, actor = request or {}, actor or {}
    events = list(events or [])
    details = dict(details or {})
    stage = traceability.request_stage_key(request, events)

    def fail(message, kind=traceability.EVENT_ORDER_ACTION_FAILED, **extra):
        return {
            "ok": False, "message": message, "failure_kind": kind,
            "failure_details": {"attempted_event": event_type, "reason": message,
                                "stage": stage, **extra},
        }

    capability = traceability.EVENT_CAPABILITIES.get(event_type)
    if capability and not permissions.has_cap(actor, capability):
        return fail("No tienes permiso para realizar esta transición.")
    target = traceability.transition_target(stage, event_type)
    if target is None:
        if event_type == traceability.EVENT_ORDER_CLAIMED and stage == traceability.STAGE_CLAIMED:
            who = assigned_picker_name(events) or "otro despachador"
            return fail(f"Este pedido ya lo tomó {who}.")
        label = traceability.EVENT_LABELS.get(event_type, event_type)
        return fail(f"«{label}» no es válido cuando la solicitud está en «{traceability.STAGES[stage][0]}».")

    is_product = request.get("request_type") == "product"
    quantity = int(request.get("quantity") or 0)
    owner = bool(actor.get("id")) and request.get("requester_id") == actor.get("id")
    assigned = assigned_picker(events)
    plan = {
        "ok": True, "message": traceability.EVENT_LABELS.get(event_type, "Transición registrada."),
        "status": None, "loan_action": None, "loan_id": "", "details": details,
        "extra_events": [], "stage": target,
    }

    if event_type == traceability.EVENT_ORDER_CANCELLED:
        if owner:
            if not permissions.has_cap(actor, permissions.CAP_CANCEL_OWN_REQUEST):
                return fail("No tienes permiso para cancelar esta solicitud.")
            if stage != traceability.STAGE_REQUESTED:
                return fail("Solo puedes cancelar tu solicitud mientras está en revisión.")
        elif not permissions.has_cap(actor, permissions.CAP_APPROVE):
            return fail("No tienes permiso para cancelar esta solicitud.")
        plan["status"] = "cancelled"
    elif event_type in {traceability.EVENT_ORDER_APPROVED, traceability.EVENT_ORDER_REJECTED}:
        approved = event_type == traceability.EVENT_ORDER_APPROVED
        pickers = _picker_list(details.pop("authorized_pickers", None))
        if approved and is_product:
            if available is None or quantity > available:
                return fail(f"Ya no hay stock suficiente. Disponible: {available or 0}.")
            plan["extra_events"].append(
                (traceability.EVENT_ORDER_AUTHORIZED, {"authorized_pickers": pickers})
            )
        plan["status"] = "approved" if approved else "rejected"
    elif event_type == traceability.EVENT_ORDER_AUTHORIZED:
        if not is_product:
            return fail("Solo las solicitudes de producto pasan por despacho.")
        details["authorized_pickers"] = _picker_list(details.get("authorized_pickers"))
    elif event_type == traceability.EVENT_ORDER_CLAIMED:
        if not is_authorized(events):
            return fail("El pedido todavía no tiene autorización de despacho.")
        if traceability.first_event(events, traceability.EVENT_PICKED_UP):
            return fail("El solicitante ya retiró este pedido por autoservicio.")
        allowed = authorized_pickers(events)
        if allowed and actor.get("id") not in allowed:
            return fail("No estás autorizado para tomar este pedido.")
        details["picker_id"] = actor.get("id", "")
        details["picker_name"] = actor.get("full_name", "")
    elif event_type == traceability.EVENT_ORDER_RELEASED:
        if assigned != actor.get("id") and not permissions.has_cap(actor, permissions.CAP_REASSIGN):
            return fail("Solo quien tomó el pedido o un encargado puede liberarlo.")
        details["picker_id"] = assigned
        details["reason"] = _clean(details.get("reason"))
    elif event_type == traceability.EVENT_ORDER_REASSIGNED:
        picker_id = str(details.get("picker_id") or "")
        if not picker_id:
            return fail("Selecciona un encargado de despacho activo.")
        if picker_id == assigned:
            return fail("El pedido ya está asignado a esa persona.")
        details["previous_picker_id"] = assigned
        details["reason"] = _clean(details.get("reason"))
    elif event_type == traceability.EVENT_ORDER_PICKED:
        if assigned != actor.get("id"):
            return fail("El pedido está asignado a otro despachador.")
        readings = details.get("readings") or []
        if len(readings) != quantity:
            return fail(f"Faltan lecturas: se requieren {quantity} y hay {len(readings)}.")
        wrong = next((row for row in readings if row.get("code") != request.get("item_id")), None)
        if wrong:
            return fail("Una lectura no corresponde al producto solicitado.",
                        kind=traceability.EVENT_ORDER_SCAN_FAILED,
                        scanned_code=str(wrong.get("code") or ""))
        if open_loan:
            # Re-picking tras «devuelto al picking»: las unidades ya salieron del
            # inventario con este préstamo; no se descuentan dos veces.
            plan["loan_action"], plan["loan_id"] = "reuse", str(open_loan.get("id") or "")
            details["reused_loan"] = True
        else:
            if available is None or quantity > available:
                return fail(f"Stock insuficiente. Disponible: {available or 0}.")
            plan["loan_action"] = "create"
        details["quantity"] = quantity
    elif event_type == traceability.EVENT_ORDER_DELIVERED:
        if not open_loan:
            return fail("No hay un préstamo abierto de este picking para entregar.")
        plan["loan_id"] = str(open_loan.get("id") or "")
        details["notes"] = _clean(details.get("notes"))
    elif event_type == traceability.EVENT_ORDER_RECEIVED:
        details["notes"] = _clean(details.get("notes"))
    elif event_type == traceability.EVENT_ORDER_REWORK:
        details["reason"] = _clean(details.get("reason"))
        if not details["reason"]:
            return fail("Indica qué debe corregirse en el picking.")
    elif event_type == traceability.EVENT_ORDER_RETURNED_TO_SHELF:
        details["reason"] = _clean(details.get("reason"))
        if not details["reason"]:
            return fail("Indica por qué se devuelve al estante.")
        if not open_loan:
            return fail("No existe un préstamo abierto para devolver al estante.")
        plan["loan_action"], plan["loan_id"] = "close", str(open_loan.get("id") or "")
    elif event_type == traceability.EVENT_ORDER_CLIENT_CONFIRMED:
        if not owner:
            return fail("Solo el solicitante puede confirmar la recepción.")
    return plan


def queue_for(storage, actor: dict) -> list:
    """Pedidos aprobados y autorizados que ``actor`` puede tomar."""
    permissions.require_cap(actor, permissions.CAP_VIEW_DISPATCH_QUEUE)
    rows = []
    for request in storage.get_service_requests(status="approved"):
        if request.get("request_type") != "product":
            continue
        events = storage.get_trace_events(request_id=request["id"])
        if traceability.request_stage_key(request, events) != traceability.STAGE_APPROVED:
            continue
        if not is_authorized(events):
            continue
        allowed = authorized_pickers(events)
        if not allowed or actor.get("id") in allowed:
            rows.append(request)
    return rows


def pending_authorization(storage, actor: dict) -> list:
    """Aprobadas sin autorización (p. ej. anteriores a v1.15) para el encargado."""
    permissions.require_cap(actor, permissions.CAP_AUTHORIZE_DISPATCH)
    rows = []
    for request in storage.get_service_requests(status="approved"):
        if request.get("request_type") != "product":
            continue
        events = storage.get_trace_events(request_id=request["id"])
        if traceability.request_stage_key(request, events) != traceability.STAGE_APPROVED:
            continue
        legacy = {event.get("event_type") for event in events} & {
            traceability.EVENT_PICKED_UP, traceability.EVENT_ROUTE_VERIFIED,
        }
        if not is_authorized(events) and not legacy:
            rows.append(request)
    return rows


def my_history(storage, actor: dict, limit: int = 20) -> list:
    """Resumen de lo que ``actor`` tomó, sin datos de otros despachadores."""
    permissions.require_cap(actor, permissions.CAP_VIEW_DISPATCH_QUEUE)
    rows = []
    for request in storage.get_service_requests():
        events = storage.get_trace_events(request_id=request["id"])
        mine = [event for event in events if event.get("actor_id") == actor.get("id")
                and event.get("event_type") in {
                    traceability.EVENT_ORDER_CLAIMED, traceability.EVENT_ORDER_RELEASED,
                    traceability.EVENT_ORDER_PICKED,
                }]
        if not mine:
            continue
        rows.append({
            "request_id": request["id"], "short_id": traceability.short_id(request["id"]),
            "item_name": request.get("item_name", ""), "quantity": request.get("quantity", 0),
            "last_action": traceability.EVENT_LABELS.get(mine[-1].get("event_type"), ""),
            "at": mine[-1].get("created_at", ""),
            "stage": traceability.STAGES[traceability.request_stage_key(request, events)][0],
        })
    rows.sort(key=lambda row: str(row["at"]), reverse=True)
    return rows[:max(1, int(limit))]


def my_orders(storage, actor: dict) -> list:
    permissions.require_cap(actor, permissions.CAP_VIEW_DISPATCH_QUEUE)
    rows = []
    for request in storage.get_service_requests(status="approved"):
        events = storage.get_trace_events(request_id=request["id"])
        stage = traceability.request_stage_key(request, events)
        if assigned_picker(events) == actor.get("id") and stage in {
            traceability.STAGE_CLAIMED, traceability.STAGE_PICKED,
        }:
            rows.append(request)
    return rows


def review_queue(storage, actor: dict) -> list:
    permissions.require_cap(actor, permissions.CAP_RECEIVE_AND_DELIVER)
    rows = []
    for request in storage.get_service_requests(status="approved"):
        events = storage.get_trace_events(request_id=request["id"])
        if traceability.request_stage_key(request, events) in {
            traceability.STAGE_PICKED, traceability.STAGE_RECEIVED,
        }:
            rows.append(request)
    return rows


def authorize_request(storage, request_id: str, actor: dict, picker_ids=None):
    permissions.require_cap(actor, permissions.CAP_AUTHORIZE_DISPATCH)
    return storage.transition_request(
        request_id, traceability.EVENT_ORDER_AUTHORIZED, actor,
        {"authorized_pickers": sorted(set(str(v) for v in (picker_ids or []) if v))},
    )


def claim_request(storage, request_id: str, actor: dict):
    permissions.require_cap(actor, permissions.CAP_CLAIM)
    return storage.transition_request(
        request_id, traceability.EVENT_ORDER_CLAIMED, actor,
        {"picker_id": actor.get("id", ""), "picker_name": actor.get("full_name", "")},
    )


def release_request(storage, request_id: str, actor: dict, reason: str = ""):
    permissions.require_cap(actor, permissions.CAP_RELEASE)
    return storage.transition_request(
        request_id, traceability.EVENT_ORDER_RELEASED, actor,
        {"picker_id": actor.get("id", ""), "reason": " ".join((reason or "").split())},
    )


def reassign_request(storage, request_id: str, actor: dict, picker: dict, reason: str = ""):
    permissions.require_cap(actor, permissions.CAP_REASSIGN)
    if not picker or picker.get("status") != "active" or picker.get("role") != permissions.ROLE_OPERATOR:
        return False, "Selecciona un encargado de despacho activo.", None
    return storage.transition_request(
        request_id, traceability.EVENT_ORDER_REASSIGNED, actor,
        {"picker_id": picker["id"], "picker_name": picker.get("full_name", ""),
         "reason": " ".join((reason or "").split())},
    )


def validate_reading(request: dict, code: str) -> tuple:
    code = (code or "").strip()
    if not code:
        return False, "La lectura está vacía."
    if code != (request or {}).get("item_id"):
        return False, f"Se leyó {code}; se esperaba {(request or {}).get('item_id') or 'el producto solicitado'}."
    return True, "Lectura correcta."


def record_failed_scan(storage, request_id: str, actor: dict, code: str, reason: str):
    permissions.require_cap(actor, permissions.CAP_PICK)
    request = storage.get_service_request(request_id)
    if not request:
        return None
    return storage.add_trace_event({
        "event_type": traceability.EVENT_ORDER_SCAN_FAILED,
        "request_id": request_id, "item_id": request.get("item_id", ""),
        "user_id": request.get("requester_id", ""), "owner_id": request.get("requester_id", ""),
        "actor_id": actor.get("id", ""), "actor_name": actor.get("full_name", ""),
        "actor_email": actor.get("institutional_email", ""), "actor_role": actor.get("role", ""),
        "details": json.dumps({"scanned_code": code, "reason": reason}, ensure_ascii=False),
    })


def complete_picking(storage, request_id: str, actor: dict, readings: list, method: str = "barcode"):
    permissions.require_cap(actor, permissions.CAP_PICK)
    clean = []
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    for value in readings or []:
        if isinstance(value, dict):
            clean.append({"code": str(value.get("code") or "").strip(),
                          "method": str(value.get("method") or method),
                          "at": str(value.get("at") or now)})
        else:
            clean.append({"code": str(value or "").strip(), "method": method, "at": now})
    return storage.transition_request(
        request_id, traceability.EVENT_ORDER_PICKED, actor,
        {"picker_id": actor.get("id", ""), "readings": clean, "method": method},
    )


def receive_request(storage, request_id: str, actor: dict, notes: str = ""):
    permissions.require_cap(actor, permissions.CAP_RECEIVE_AND_DELIVER)
    return storage.transition_request(
        request_id, traceability.EVENT_ORDER_RECEIVED, actor,
        {"notes": " ".join((notes or "").split())},
    )


def deliver_request(storage, request_id: str, actor: dict, notes: str = ""):
    permissions.require_cap(actor, permissions.CAP_RECEIVE_AND_DELIVER)
    return storage.transition_request(
        request_id, traceability.EVENT_ORDER_DELIVERED, actor,
        {"notes": " ".join((notes or "").split())},
    )


def rework_request(storage, request_id: str, actor: dict, reason: str):
    permissions.require_cap(actor, permissions.CAP_RECEIVE_AND_DELIVER)
    reason = " ".join((reason or "").split())
    if not reason:
        return False, "Indica qué debe corregirse en el picking.", None
    return storage.transition_request(
        request_id, traceability.EVENT_ORDER_REWORK, actor, {"reason": reason},
    )


def return_to_shelf(storage, request_id: str, actor: dict, reason: str):
    permissions.require_cap(actor, permissions.CAP_RECEIVE_AND_DELIVER)
    reason = " ".join((reason or "").split())
    if not reason:
        return False, "Indica por qué se devuelve al estante.", None
    return storage.transition_request(
        request_id, traceability.EVENT_ORDER_RETURNED_TO_SHELF, actor, {"reason": reason},
    )


def confirm_client_receipt(storage, request_id: str, actor: dict):
    permissions.require_cap(actor, permissions.CAP_CONFIRM_RECEIPT)
    return storage.transition_request(
        request_id, traceability.EVENT_ORDER_CLIENT_CONFIRMED, actor, {},
    )


def dispatch_metrics(storage, actor: dict) -> list:
    permissions.require_cap(actor, permissions.CAP_DISPATCH_REPORTS)
    rows = []
    for request in storage.get_service_requests():
        events = storage.get_trace_events(request_id=request["id"])
        if request.get("request_type") != "product" or not events:
            continue
        by_type = {event.get("event_type"): event for event in events}
        requested = traceability.to_datetime(request.get("created_at"))
        delivered = traceability.to_datetime((by_type.get(traceability.EVENT_ORDER_DELIVERED) or {}).get("created_at"))
        claimed = traceability.to_datetime((by_type.get(traceability.EVENT_ORDER_CLAIMED) or {}).get("created_at"))
        picked = traceability.to_datetime((by_type.get(traceability.EVENT_ORDER_PICKED) or {}).get("created_at"))
        rows.append({
            "request_id": request["id"], "stage": traceability.request_stage_key(request, events),
            "requested_at": request.get("created_at", ""),
            "claim_seconds": int((claimed - requested).total_seconds()) if requested and claimed else "",
            "picking_seconds": int((picked - claimed).total_seconds()) if claimed and picked else "",
            "delivery_seconds": int((delivered - requested).total_seconds()) if requested and delivered else "",
            "picker_role": (by_type.get(traceability.EVENT_ORDER_PICKED) or {}).get("actor_role", ""),
        })
    return rows


def dispatch_csv(storage, actor: dict, salt: str = "dispatch-report") -> bytes:
    rows = dispatch_metrics(storage, actor)
    output = io.StringIO(newline="")
    fields = ["request_id", "stage", "requested_at", "claim_seconds", "picking_seconds",
              "delivery_seconds", "picker_role"]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        row = dict(row)
        row["request_id"] = hashlib.sha256(f"{salt}:{row['request_id']}".encode()).hexdigest()[:12]
        writer.writerow(row)
    return output.getvalue().encode("utf-8-sig")
