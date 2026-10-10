# -*- coding: utf-8 -*-
"""Flujo de solicitudes de productos y servicios del laboratorio."""

from datetime import datetime, timezone

from core import notifications
from core.reservations import as_utc

TYPE_PRODUCT = "product"
TYPE_SERVICE = "service"
VALID_TYPES = (TYPE_PRODUCT, TYPE_SERVICE)
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED = "cancelled"
REVIEWER_ROLES = ("profesor", "maestro")


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
    data = validate_request(
        storage, request_type, item_id, quantity, service_name, description, needed_at
    )
    row = storage.create_service_request(data, user)
    recipients = notifications.get_admin_notification_emails(storage)
    target = (
        f"Producto: {row['item_name']} ({row['item_id']}) x{row['quantity']}"
        if row["request_type"] == TYPE_PRODUCT
        else f"Servicio: {row['service_name']}"
    )
    subject = f"Nueva solicitud de laboratorio: {target}"
    body = (
        f"Solicitud #{row['id']}\n"
        f"Solicitante: {row['requester_name']} <{row['requester_email']}>\n"
        f"{target}\nFecha requerida (UTC): {row.get('needed_at') or 'No indicada'}\n"
        f"Descripción: {row.get('description') or 'Sin notas'}\n\n"
        "Ingresa a la aplicación para aprobar o rechazar la solicitud."
    )
    notified, message = notifications.send_email_notification(subject, body, recipients)
    storage.update_service_request_notification(row["id"], notified, "" if notified else message)
    row["email_notified"], row["email_error"] = notified, "" if notified else message
    return row, notified, message


def review_request(storage, request_id: str, decision: str, reviewer: dict, notes: str = ""):
    if reviewer.get("role") not in REVIEWER_ROLES:
        return False, "Solo profesor o maestro pueden revisar solicitudes."
    if decision not in (STATUS_APPROVED, STATUS_REJECTED):
        return False, "La decisión debe ser approved o rejected."
    request = storage.get_service_request(request_id)
    if not request:
        return False, "La solicitud no existe."
    if request.get("status") != STATUS_PENDING:
        return False, "Solo se pueden revisar solicitudes pendientes."
    if decision == STATUS_APPROVED and request.get("request_type") == TYPE_PRODUCT:
        available = storage.get_available_quantity(request.get("item_id", ""))
        if int(request.get("quantity") or 0) > available:
            return False, f"Ya no hay stock suficiente. Disponible: {available}."
    storage.update_service_request_status(
        request_id, decision, reviewer.get("institutional_email", ""), notes
    )
    return True, "Solicitud aprobada." if decision == STATUS_APPROVED else "Solicitud rechazada."


def cancel_request(storage, request_id: str, actor: dict):
    request = storage.get_service_request(request_id)
    if not request:
        return False, "La solicitud no existe."
    owner = request.get("requester_id") == actor.get("id")
    privileged = actor.get("role") in REVIEWER_ROLES
    if not (owner or privileged):
        return False, "No tienes permiso para cancelar esta solicitud."
    if request.get("status") in (STATUS_REJECTED, STATUS_CANCELLED):
        return False, "La solicitud ya está cerrada."
    storage.update_service_request_status(
        request_id, STATUS_CANCELLED, actor.get("institutional_email", ""), "Cancelada"
    )
    return True, "Solicitud cancelada."