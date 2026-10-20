# -*- coding: utf-8 -*-
"""Roles técnicos, etiquetas visibles y autorización por capacidades.

Las etiquetas se pueden renombrar sin alterar las reglas. Las vistas ocultan
acciones no disponibles, pero cada función de dominio vuelve a validar la
capacidad correspondiente.
"""

ROLE_STUDENT = "estudiante"
ROLE_OPERATOR = "operario"
ROLE_ATTENDANT = "encargado"
ROLE_PROFESSOR = "profesor"
ROLE_MASTER = "maestro"
ROLES = (ROLE_STUDENT, ROLE_OPERATOR, ROLE_ATTENDANT, ROLE_PROFESSOR, ROLE_MASTER)

# Única fuente de las etiquetas visibles; los ids técnicos de arriba no cambian.
# Profesor y encargado comparten etiqueta («Supervisor»): se distinguen por sus
# capacidades (el encargado no edita inventario) y, donde importa, por
# role_detail_label().
ROLE_LABELS = {
    ROLE_STUDENT: "Cliente",
    ROLE_OPERATOR: "Operario",
    ROLE_ATTENDANT: "Supervisor",
    ROLE_PROFESSOR: "Supervisor",
    ROLE_MASTER: "Administrador",
}

CAP_REQUEST = "request"
CAP_VIEW_OWN_REQUESTS = "view_own_requests"
CAP_CANCEL_OWN_REQUEST = "cancel_own_request"
CAP_CONFIRM_RECEIPT = "confirm_receipt"
CAP_RESERVE = "reserve"
CAP_CHECKOUT = "checkout"
CAP_VIEW_ALL_LOANS = "view_all_loans"
CAP_CHECKIN_ANY = "checkin_any_loan"
CAP_APPROVE = "approve"
CAP_AUTHORIZE_DISPATCH = "authorize_dispatch"
CAP_VIEW_DISPATCH_QUEUE = "view_dispatch_queue"
CAP_CLAIM = "claim"
CAP_RELEASE = "release"
CAP_PICK = "pick"
CAP_RECEIVE_AND_DELIVER = "receive_and_deliver"
CAP_REASSIGN = "reassign"
CAP_VIEW_ALL_TRACES = "view_all_traces"
CAP_EDIT_INVENTORY = "edit_inventory"
CAP_MANAGE_USERS = "manage_users"
CAP_MANAGE_NFC = "manage_nfc"
CAP_MANAGE_LOCATIONS = "manage_locations"
CAP_REPORTS = "reports"
CAP_DISPATCH_REPORTS = "dispatch_reports"
CAP_MANAGE_EMAIL = "manage_email"
CAP_ADJUST_COUNTS = "adjust_counts"
CAP_MANAGE_RESERVATIONS = "manage_reservations"
CAP_DELETE_PHOTOS = "delete_photos"

_CLIENT = {
    CAP_REQUEST, CAP_VIEW_OWN_REQUESTS, CAP_CANCEL_OWN_REQUEST,
    CAP_CONFIRM_RECEIPT, CAP_RESERVE, CAP_CHECKOUT,
}
_DISPATCH = {CAP_VIEW_DISPATCH_QUEUE, CAP_CLAIM, CAP_RELEASE, CAP_PICK}
_ATTEND = {
    CAP_APPROVE, CAP_AUTHORIZE_DISPATCH, CAP_RECEIVE_AND_DELIVER,
    CAP_REASSIGN, CAP_RELEASE, CAP_VIEW_ALL_TRACES, CAP_DISPATCH_REPORTS,
}
_PROFESSOR = {
    CAP_EDIT_INVENTORY, CAP_MANAGE_NFC, CAP_MANAGE_LOCATIONS,
    CAP_REPORTS, CAP_MANAGE_RESERVATIONS,
    CAP_VIEW_ALL_LOANS, CAP_CHECKIN_ANY,
}
_ADMIN = {CAP_MANAGE_USERS, CAP_MANAGE_EMAIL, CAP_ADJUST_COUNTS, CAP_DELETE_PHOTOS}

ROLE_CAPS = {
    ROLE_STUDENT: frozenset(_CLIENT),
    ROLE_OPERATOR: frozenset(_DISPATCH),
    ROLE_ATTENDANT: frozenset(_CLIENT | _ATTEND),
    ROLE_PROFESSOR: frozenset(_CLIENT | _ATTEND | _PROFESSOR),
    ROLE_MASTER: frozenset(_CLIENT | _DISPATCH | _ATTEND | _PROFESSOR | _ADMIN),
}

# Compatibilidad para módulos externos; el código nuevo usa capacidades.
MANAGER_ROLES = (ROLE_ATTENDANT, ROLE_PROFESSOR, ROLE_MASTER)
ADMIN_ROLES = (ROLE_MASTER,)


def has_role(user: dict, roles) -> bool:
    return bool(user) and user.get("role") in roles


def has_cap(user: dict, capability: str) -> bool:
    """True si el rol técnico actual concede ``capability``."""
    return bool(user) and capability in ROLE_CAPS.get(user.get("role"), ())


def require_cap(user: dict, capability: str, message: str = "No tienes permiso para esta acción.") -> None:
    """Autorización de dominio; nunca debe sustituirse por ocultar un botón."""
    if not has_cap(user, capability):
        raise PermissionError(message)


def role_detail_label(role_or_user) -> str:
    """Etiqueta para quien asigna roles: si dos roles comparten etiqueta, agrega el id."""
    role = role_or_user.get("role", "") if isinstance(role_or_user, dict) else str(role_or_user or "")
    label = ROLE_LABELS.get(role, role or "Sin rol")
    shared = sum(1 for value in ROLE_LABELS.values() if value == label) > 1
    return f"{label} ({role})" if shared else label


def role_label(role_or_user) -> str:
    role = role_or_user.get("role", "") if isinstance(role_or_user, dict) else str(role_or_user or "")
    return ROLE_LABELS.get(role, role or "Sin rol")
