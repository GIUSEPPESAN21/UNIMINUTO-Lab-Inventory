# -*- coding: utf-8 -*-
"""core/permissions.py - Roles y comprobaciones de permiso centralizados.

La navegacion ya oculta las paginas segun el rol, pero ocultar no es autorizar:
cada vista sensible vuelve a comprobar el rol (ver `core.ui.guard_role`) para que
una sesion con el rol equivocado, o una URL escrita a mano, no llegue a ellas."""

ROLE_STUDENT = "estudiante"
ROLE_PROFESSOR = "profesor"
ROLE_MASTER = "maestro"

# Gestionan inventario, aprueban solicitudes/reservas y ven reportes.
MANAGER_ROLES = (ROLE_PROFESSOR, ROLE_MASTER)
# Administran usuarios, roles y lista blanca.
ADMIN_ROLES = (ROLE_MASTER,)


def has_role(user: dict, roles) -> bool:
    return bool(user) and user.get("role") in roles
