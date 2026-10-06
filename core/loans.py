# -*- coding: utf-8 -*-
"""
core/loans.py - Logica de negocio de salida (checkout) y reingreso (checkin).

La cantidad total de un item (items.quantity) solo cambia por Alta/Ajuste/Baja.
La disponibilidad se calcula en vivo como quantity - suma(prestamos abiertos),
de forma que siempre queda una auditoria completa de quien tiene que en 'loans'.
"""

import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

BOGOTA_TZ = ZoneInfo("America/Bogota")


def deadline_from_date(day: date) -> datetime:
    """Fecha limite de devolucion elegida en la interfaz (solo dia, sin hora).

    Se guarda como medianoche UTC de ese dia: ese valor exacto es la marca de
    "fecha sin hora" que `due_instant` interpreta (y que ya usan los prestamos
    registrados antes de esta version)."""
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


def due_instant(expected: datetime) -> datetime:
    """Instante a partir del cual un prestamo se considera vencido.

    Una fecha limite sin hora (medianoche UTC exacta) vence al TERMINAR ese dia
    en Colombia, no a las 7 p. m. del dia anterior (medianoche UTC). Una fecha
    con hora concreta se respeta tal cual."""
    if expected.tzinfo is None:
        expected = expected.replace(tzinfo=timezone.utc)
    expected = expected.astimezone(timezone.utc)
    if expected.timetz().replace(tzinfo=None) == time.min:
        return datetime.combine(expected.date() + timedelta(days=1), time.min, tzinfo=BOGOTA_TZ)
    return expected


def checkout(storage, item_id: str, quantity: int, user: dict, expected_return_at=None, notes: str = ""):
    if quantity <= 0:
        return False, "La cantidad debe ser mayor a 0.", None

    item = storage.get_item(item_id)
    if not item:
        return False, f"El item '{item_id}' no existe.", None
    if item.get("item_type") == "master":
        return False, "No se puede dar salida a un Contenedor Principal, solo a los items que tiene dentro.", None

    available = storage.get_available_quantity(item_id)
    if quantity > available:
        return False, f"Stock insuficiente. Disponible: {available}.", None

    try:
        loan = storage.create_loan(item, quantity, user, expected_return_at=expected_return_at, notes=notes)
    except ValueError as exc:
        # La capa de datos re-verifica el stock dentro de su lock: otra persona
        # pudo sacar unidades entre la comprobacion anterior y este punto.
        return False, str(exc), None
    return True, f"Salida registrada: '{item.get('name')}' x{quantity} para {user.get('full_name')}.", loan


def checkin(storage, loan_id: str, actor_user: dict):
    ok, msg = storage.return_loan(loan_id, actor_email=actor_user.get("institutional_email", ""))
    return ok, msg


def is_overdue(loan: dict, now: datetime = None) -> bool:
    if loan.get("status") != "out":
        return False
    expected = loan.get("expected_return_at")
    if not expected:
        return False
    return due_instant(expected) < (now or datetime.now(timezone.utc))


def get_overdue_loans(storage) -> list:
    open_loans = storage.get_all_loans(status="out")
    return [l for l in open_loans if is_overdue(l)]


def get_loans_for_view(storage, user: dict) -> list:
    """Estudiante ve solo lo suyo; profesor/maestro ven todos los prestamos abiertos."""
    if user.get("role") == "estudiante":
        return storage.get_open_loans_for_user(user["id"])
    return storage.get_all_loans(status="out")
