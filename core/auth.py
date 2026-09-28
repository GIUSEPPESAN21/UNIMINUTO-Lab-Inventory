# -*- coding: utf-8 -*-
"""
core/auth.py - Registro, inicio de sesion y control de roles.

Regla de seguridad clave: el rol NUNCA se elige libremente en el registro.
- Toda cuenta nueva nace "estudiante".
- Nace "profesor" solo si su correo ya esta en la lista blanca (professors_whitelist),
  que unicamente un "maestro" puede editar.
- El rol "maestro" nunca se auto-asigna: la primera cuenta maestra se siembra desde
  st.secrets (MASTER_EMAIL / MASTER_INITIAL_PASSWORD) si todavia no existe ninguna.

No se restringe el registro a un dominio institucional especifico (asi otros
laboratorios pueden usar la misma app con su propio correo institucional):
cualquier correo cuyo dominio termine en ".edu" o ".edu.co" es valido.
"""

import logging

import bcrypt

from core.config import safe_secret

logger = logging.getLogger(__name__)

INSTITUTIONAL_EMAIL_SUFFIXES = (".edu", ".edu.co")


def is_institutional_email(email: str) -> bool:
    email = (email or "").strip().lower()
    if "@" not in email:
        return False
    domain = email.split("@")[-1]
    return domain.endswith(INSTITUTIONAL_EMAIL_SUFFIXES)


def hash_password(plain_password: str) -> str:
    return bcrypt.hashpw(plain_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def ensure_master_seed(storage) -> None:
    """Crea la primera cuenta maestra desde los Secrets si aun no existe ninguna."""
    try:
        if storage.count_masters() > 0:
            return
        master_email = safe_secret("MASTER_EMAIL", "")
        master_password = safe_secret("MASTER_INITIAL_PASSWORD", "")
        if not master_email or not master_password:
            return
        if storage.get_user_by_email(master_email):
            return
        storage.create_user(
            full_name="Administrador del Laboratorio",
            email=master_email,
            password_hash=hash_password(master_password),
            role="maestro",
            program="Direccion de Laboratorio - UNIMINUTO",
            status="active",
        )
        logger.info("Cuenta maestra sembrada desde Secrets.")
    except Exception as e:
        logger.error(f"No se pudo sembrar la cuenta maestra: {e}")


def register_user(storage, full_name: str, email: str, password: str, program: str, student_id: str):
    email = (email or "").strip().lower()
    full_name = (full_name or "").strip()
    program = (program or "").strip()
    student_id = (student_id or "").strip()

    if len(full_name.split()) < 2:
        return None, "Ingresa tu nombre completo (nombre y apellido)."
    if not is_institutional_email(email):
        return None, "Debes registrarte con un correo institucional valido (terminado en .edu o .edu.co)."
    if not student_id:
        return None, "El ID de estudiante es obligatorio."
    if not program:
        return None, "El programa academico o departamento es obligatorio."
    if not password or len(password) < 8:
        return None, "La contrasena debe tener al menos 8 caracteres."
    if storage.get_user_by_email(email):
        return None, "Ya existe una cuenta registrada con ese correo."

    role = "profesor" if storage.is_email_whitelisted_professor(email) else "estudiante"

    user = storage.create_user(
        full_name=full_name,
        email=email,
        password_hash=hash_password(password),
        role=role,
        program=program,
        student_id=student_id,
        status="active",
    )
    return user, None


def login_user(storage, email: str, password: str):
    email = (email or "").strip().lower()
    user = storage.get_user_by_email(email)
    if not user:
        return None, "No existe una cuenta con ese correo."
    if user.get("status") != "active":
        return None, "Tu cuenta esta deshabilitada. Contacta a un administrador."
    if not verify_password(password, user.get("password_hash", "")):
        return None, "Contrasena incorrecta."
    return user, None
