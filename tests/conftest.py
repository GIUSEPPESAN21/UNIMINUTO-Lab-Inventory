# -*- coding: utf-8 -*-
"""Fixtures compartidas: un FakeStorage en memoria que cumple el mismo
'contrato' publico que core.storage.LabStorage, sin tocar pandas/Excel/GitHub,
para poder probar la logica de negocio de core/*.py de forma aislada y rapida.
"""

import uuid
from datetime import datetime, timezone

import pytest


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path, monkeypatch):
    """Aísla TODA prueba del disco y de la red reales.

    - El Excel de trabajo vive en `tmp_path`: ninguna prueba escribe en la raíz
      del repositorio ni depende de lo que dejó otra corrida.
    - `safe_secret` devuelve siempre el valor por defecto: un
      `.streamlit/secrets.toml` real en la máquina del desarrollador nunca
      activa la sincronización con GitHub ni el envío de correos.
    - Se reinician la caché en memoria y el estado de sincronización/sesión.
    """
    from core import auth, notifications
    from core import storage as storage_module

    monkeypatch.setattr(storage_module, "EXCEL_PATH", str(tmp_path / "isolated_db.xlsx"))
    monkeypatch.setattr(storage_module, "_cached_dfs", None)

    def no_secret(key, default=None):
        return default

    for module in (storage_module, auth, notifications):
        monkeypatch.setattr(module, "safe_secret", no_secret)
    storage_module._reset_sync_state()
    auth.reset_login_throttle()
    yield
    storage_module._cached_dfs = None


class FakeStorage:
    def __init__(self):
        self.items = {}
        self.users = {}
        self.whitelist = set()
        self.loans = {}
        self.reservations = {}
        self.service_requests = {}
        self.settings = {}
        self.trace_events = []
        self.history = []

    # --- items ---
    def add_item(self, item_id, **kwargs):
        base = {
            "id": item_id, "name": "Item", "category": "", "description": "",
            "item_type": "standalone", "parent_id": "", "unit": "unidad",
            "quantity": 0, "location": "", "min_stock_alert": 0, "status": "active",
            "created_by": "", "updated_at": "",
        }
        base.update(kwargs)
        self.items[item_id] = base
        return base

    def get_item(self, item_id):
        return dict(self.items[item_id]) if item_id in self.items else None

    def get_children(self, parent_id):
        return [dict(i) for i in self.items.values() if i.get("parent_id") == parent_id and i.get("status") != "retired"]

    def get_all_masters(self):
        return [dict(i) for i in self.items.values() if i.get("item_type") == "master" and i.get("status") != "retired"]

    def get_available_quantity(self, item_id):
        item = self.get_item(item_id)
        if not item:
            return 0
        borrowed = sum(l["quantity"] for l in self.loans.values() if l["item_id"] == item_id and l["status"] == "out")
        return max(item.get("quantity", 0) - borrowed, 0)

    # --- users ---
    def add_user(self, email, **kwargs):
        base = {
            "id": uuid.uuid4().hex[:10], "full_name": "Usuario", "student_id": "",
            "institutional_email": email.lower(),
            "password_hash": "", "role": "estudiante", "program_or_department": "",
            "status": "active", "created_at": "",
        }
        base.update(kwargs)
        self.users[base["id"]] = base
        return base

    def get_user_by_email(self, email):
        for u in self.users.values():
            if u["institutional_email"] == email.lower().strip():
                return dict(u)
        return None

    def get_user_by_id(self, user_id):
        u = self.users.get(user_id)
        return dict(u) if u else None

    def create_user(self, full_name, email, password_hash, role, program="", student_id="", status="active"):
        return self.add_user(
            email, full_name=full_name, password_hash=password_hash, role=role,
            program_or_department=program, student_id=student_id, status=status,
        )

    def update_user(self, user_id, changes):
        self.users[user_id].update(changes)
        return dict(self.users[user_id])

    def get_all_users(self):
        return [dict(user) for user in self.users.values()]

    def count_masters(self):
        return len([u for u in self.users.values() if u["role"] == "maestro"])

    # --- whitelist ---
    def is_email_whitelisted_professor(self, email):
        return email.lower().strip() in self.whitelist

    def add_to_whitelist(self, email):
        self.whitelist.add(email.lower().strip())

    def get_availability_map(self):
        return {item_id: self.get_available_quantity(item_id) for item_id in self.items}

    # --- loans ---
    def create_loan(self, item, quantity, user, expected_return_at=None, notes=""):
        available = self.get_available_quantity(item["id"]) if item["id"] in self.items else None
        if available is not None and quantity > available:
            raise ValueError(f"Stock insuficiente. Disponible: {available}.")
        loan_id = uuid.uuid4().hex[:10]
        loan = {
            "id": loan_id, "item_id": item["id"], "item_name": item.get("name", ""),
            "parent_id": item.get("parent_id", ""), "quantity": quantity,
            "user_id": user["id"], "user_name": user.get("full_name", ""),
            "user_role": user.get("role", ""), "checkout_at": datetime.now(timezone.utc),
            "expected_return_at": expected_return_at, "return_at": None,
            "status": "out", "notes": notes,
        }
        self.loans[loan_id] = loan
        return loan

    def return_loan(self, loan_id, actor_email=""):
        loan = self.loans.get(loan_id)
        if not loan:
            return False, "El prestamo no existe."
        if loan["status"] == "returned":
            return False, "Este prestamo ya fue devuelto."
        loan["status"] = "returned"
        loan["return_at"] = datetime.now(timezone.utc)
        return True, "Reingreso registrado correctamente."

    def get_open_loans_for_item(self, item_id):
        return [dict(l) for l in self.loans.values() if l["item_id"] == item_id and l["status"] == "out"]

    def get_open_loans_for_user(self, user_id):
        return [dict(l) for l in self.loans.values() if l["user_id"] == user_id and l["status"] == "out"]

    def get_all_loans(self, status=None):
        loans = list(self.loans.values())
        if status:
            loans = [l for l in loans if l["status"] == status]
        return [dict(l) for l in loans]


    # --- reservations ---
    def create_reservation(self, data, user):
        row = {
            **data, "id": uuid.uuid4().hex[:10], "requester_id": user["id"],
            "requester_name": user.get("full_name", ""),
            "requester_email": user.get("institutional_email", ""),
            "status": "pending", "reviewed_by": "", "review_notes": "",
            "email_notified": False, "email_error": "",
        }
        self.reservations[row["id"]] = row
        return dict(row)

    def get_reservation(self, reservation_id):
        row = self.reservations.get(reservation_id)
        return dict(row) if row else None

    def get_reservations(self, status=None, user_id=None):
        rows = list(self.reservations.values())
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if user_id:
            rows = [row for row in rows if row.get("requester_id") == user_id]
        return [dict(row) for row in rows]

    def update_reservation_status(self, reservation_id, status, reviewer_email, notes=""):
        row = self.reservations.get(reservation_id)
        if not row:
            return False
        row.update(status=status, reviewed_by=reviewer_email, review_notes=notes)
        return True

    def update_reservation_notification(self, reservation_id, sent, error=""):
        row = self.reservations.get(reservation_id)
        if not row:
            return False
        row.update(email_notified=bool(sent), email_error=error)
        return True

    # --- service requests ---
    def create_service_request(self, data, user):
        row = {
            **data, "id": uuid.uuid4().hex[:10], "requester_id": user["id"],
            "requester_name": user.get("full_name", ""),
            "requester_email": user.get("institutional_email", ""),
            "status": "pending", "reviewed_by": "", "review_notes": "",
            "email_notified": False, "email_error": "",
        }
        self.service_requests[row["id"]] = row
        return dict(row)

    def get_service_request(self, request_id):
        row = self.service_requests.get(request_id)
        return dict(row) if row else None

    def get_service_requests(self, status=None, user_id=None):
        rows = list(self.service_requests.values())
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if user_id:
            rows = [row for row in rows if row.get("requester_id") == user_id]
        return [dict(row) for row in rows]

    def update_service_request_status(self, request_id, status, reviewer_email, notes=""):
        row = self.service_requests.get(request_id)
        if not row:
            return False
        row.update(status=status, reviewed_by=reviewer_email, review_notes=notes)
        return True

    def update_service_request_notification(self, request_id, sent, error=""):
        row = self.service_requests.get(request_id)
        if not row:
            return False
        row.update(email_notified=bool(sent), email_error=error)
        return True

    # --- settings ---
    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value, actor_email=""):
        self.settings[key] = value

    # --- trazabilidad ---
    def add_trace_event(self, event):
        columns = (
            "event_type", "request_id", "item_id", "loan_id", "user_id", "actor_id",
            "actor_name", "actor_email", "receipt", "details", "created_at",
        )
        row = {column: "" for column in columns}
        row.update({key: value for key, value in event.items() if key in columns})
        row["id"] = uuid.uuid4().hex[:10]
        row["created_at"] = row["created_at"] or datetime.now(timezone.utc).isoformat()
        self.trace_events.append(row)
        return dict(row)

    def get_trace_events(self, request_id=None, item_id=None, user_id=None, event_type=None,
                         loan_id=None, receipt=None):
        filters = {"request_id": request_id, "item_id": item_id, "user_id": user_id,
                   "event_type": event_type, "loan_id": loan_id, "receipt": receipt}
        rows = [
            dict(row) for row in self.trace_events
            if all(value is None or row.get(key) == value for key, value in filters.items())
        ]
        return sorted(rows, key=lambda row: row.get("created_at") or "")

    def get_item_history(self, item_id):
        return [dict(row) for row in self.history if row.get("item_id") == item_id]


@pytest.fixture
def storage():
    return FakeStorage()
