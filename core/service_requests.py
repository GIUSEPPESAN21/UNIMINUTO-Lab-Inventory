# -*- coding: utf-8 -*-
"""Flujo de solicitudes de productos y servicios del laboratorio."""

from datetime import datetime, timezone

from core import email_events, permissions
from core.config import safe_secret
from core.reservations import as_utc

TYPE_PRODUCT = "product"
TYPE_SERVICE = "service"
VALID_TYPES = (TYPE_PRODUCT, TYPE_SERVICE)
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED = "cancelled"
REVIEWER_ROLES = permissions.MANAGER_ROLES


def auto_approve_products() -> bool:
    return str(safe_secret("AUTO_APPROVE_PRODUCT_REQUESTS", "false") or "").strip().lower() in {
        "1", "true", "yes", "si", "sí",
    }


def validate_request(storage, request_type: str, item_id: str = "", quantity=1,
                     service_name: str = "", description: str = "", needed_at=None) -> dict:
    request_type = (request_type or "").strip()
    item_id = (item_id or "").strip()
    service_name = " ".join((service_name or "").strip().split())
    description = " ".join((description or "").strip().split())
    if request_type not in VALID_TYPES:
        raise ValueError("Selecciona una solicitud de producto o de servicio.")

    item = None
    if request_type == TYPE_PRODUCT:
        item = storage.get_item(item_id)
        if not item or item.get("status") == "retired":
            raise ValueError("Selecciona un producto activo del inventario.")
        if item.get("item_type") == "master":
            raise ValueError("Solicita un producto, no un Contenedor Principal.")
        if item.get("item_type") == "location":
            raise ValueError("Solicita un producto, no una ubicación (estantería, piso, mesa o zona).")
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            raise ValueError("La cantidad debe ser un número entero.") from None
        if quantity < 1:
            raise ValueError("La cantidad debe ser mayor a cero.")
        available = storage.get_available_quantity(item_id)
        if quantity > available:
            raise ValueError(f"Stock insuficiente. Disponible: {available}.")
        service_name = ""
    else:
        if not service_name:
            raise ValueError("Indica el servicio requerido.")
        if not description:
            raise ValueError("Describe la necesidad o el resultado esperado del servicio.")
        quantity = 0
        item_id = ""

    needed = ""
    if needed_at:
        needed_dt = as_utc(needed_at, "Fecha requerida")
        if needed_dt <= datetime.now(timezone.utc):
            raise ValueError("La fecha requerida debe estar en el futuro.")
        needed = needed_dt.isoformat()

    return {
        "request_type": request_type,
        "item_id": item_id,
        "item_name": item.get("name", "") if item else "",
        "quantity": quantity,
        "service_name": service_name,
        "description": description,
        "needed_at": needed,
    }


def submit_request(storage, user: dict, request_type: str, item_id: str = "", quantity=1,
                   service_name: str = "", description: str = "", needed_at=None):
    permissions.require_cap(user, permissions.CAP_REQUEST, "No tienes permiso para crear solicitudes.")
    data = validate_request(
        storage, request_type, item_id, quantity, service_name, description, needed_at
    )
    row = storage.create_service_request(data, user)
    if request_type == TYPE_PRODUCT and auto_approve_products():
        system_actor = {
            "id": "system:auto-approve", "full_name": "Aprobación automática",
            "institutional_email": "", "role": permissions.ROLE_MASTER,
        }
        storage.transition_request(
            row["id"], "order_approved", system_actor,
            {"notes": "Aprobación automática", "authorized_pickers": []},
        )
        row = storage.get_service_request(row["id"])
    notified, message = email_events.notify_request_created(storage, row, user)
    row["email_notified"], row["email_error"] = notified, "" if notified else message
    return row, notified, message


def review_request(storage, request_id: str, decision: str, reviewer: dict, notes: str = "",
                   authorized_pickers=None):
    if not permissions.has_cap(reviewer, permissions.CAP_APPROVE):
        return False, "No tienes permiso para revisar solicitudes."
    if decision not in (STATUS_APPROVED, STATUS_REJECTED):
        return False, "La decisión debe ser approved o rejected."
    request = storage.get_service_request(request_id)
    if not request:
        return False, "La solicitud no existe."
    event_type = "order_approved" if decision == STATUS_APPROVED else "order_rejected"
    ok, message, _ = storage.transition_request(
        request_id, event_type, reviewer,
        {"notes": " ".join((notes or "").split()),
         "authorized_pickers": list(authorized_pickers or [])},
    )
    if not ok:
        return False, message
    request = storage.get_service_request(request_id) or request
    email_events.notify_request_reviewed(storage, request, decision, reviewer, notes)
    return True, "Solicitud aprobada." if decision == STATUS_APPROVED else "Solicitud rechazada."


def cancel_request(storage, request_id: str, actor: dict):
    request = storage.get_service_request(request_id)
    if not request:
        return False, "La solicitud no existe."
    owner = request.get("requester_id") == actor.get("id")
    if owner and not permissions.has_cap(actor, permissions.CAP_CANCEL_OWN_REQUEST):
        return False, "No tienes permiso para cancelar esta solicitud."
    ok, message, _ = storage.transition_request(
        request_id, "order_cancelled", actor, {"reason": "Cancelada"}
    )
    return ok, "Solicitud cancelada." if ok else message