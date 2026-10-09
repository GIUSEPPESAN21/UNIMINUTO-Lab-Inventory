# -*- coding: utf-8 -*-
"""
core/auth.py - Registro, inicio de sesion y control de roles.

Regla de seguridad clave: el rol NUNCA se elige libremente en el registro.
- Toda cuenta nueva nace "estudiante".
- Nace "profesor" solo si su correo esta en la lista blanca (professors_whitelist),
  que unicamente un "maestro" puede editar, Y el solicitante demostro ser dueño
  del correo escribiendo un codigo de un solo uso enviado a esa direccion
  (sin esa prueba, cualquiera podria registrarse con el correo de un profesor).
  Si el correo no puede verificarse (SMTP sin configurar), la cuenta nace
  estudiante y el maestro puede activar el rol desde la pagina Usuarios.
- El rol "maestro" nunca se auto-asigna: la primera cuenta maestra se siembra desde
  st.secrets (MASTER_EMAIL / MASTER_INITIAL_PASSWORD) si todavia no existe ninguna.

No se restringe el registro a un dominio institucional especifico (asi otros
laboratorios pueden usar la misma app con su propio correo institucional):
cualquier correo cuyo dominio termine en ".edu" o ".edu.co" es valido.

Inicio de sesion: el mensaje de error es el mismo para "no existe la cuenta" y
"contraseña incorrecta" (no permite descubrir que correos estan registrados) y
hay un limite de intentos fallidos por correo con bloqueo temporal.
"""

import hashlib
import hmac
import logging
import math
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

import bcrypt

from core import notifications
from core.config import safe_secret

logger = logging.getLogger(__name__)

INSTITUTIONAL_EMAIL_SUFFIXES = (".edu", ".edu.co")

GENERIC_LOGIN_ERROR = "Correo o contraseña incorrectos."
DISABLED_ACCOUNT_ERROR = "Tu cuenta esta deshabilitada. Contacta a un administrador."

DEFAULT_LOGIN_MAX_ATTEMPTS = 5
DEFAULT_LOGIN_LOCKOUT_MINUTES = 5
DEFAULT_SESSION_TIMEOUT_MINUTES = 720  # 12 horas
VERIFICATION_TTL_SECONDS = 15 * 60
VERIFICATION_MAX_ATTEMPTS = 5

_MAX_TRACKED_EMAILS = 5000


def is_institutional_email(email: str) -> bool:
    email = (email or "").strip().lower()
    if "@" not in email:
        return False
    domain = email.split("@")[-1]
    return domain.endswith(INSTITUTIONAL_EMAIL_SUFFIXES)


# bcrypt solo usa los primeros 72 bytes de la contraseña. bcrypt 4 los recortaba
# en silencio; desde bcrypt 5 una contraseña mas larga lanza ValueError. Se recorta
# igual que antes para no romper el registro ni los hashes ya guardados.
BCRYPT_MAX_BYTES = 72


def _password_bytes(plain_password: str) -> bytes:
    return plain_password.encode("utf-8")[:BCRYPT_MAX_BYTES]


def hash_password(plain_password: str) -> str:
    return bcrypt.hashpw(_password_bytes(plain_password), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_password_bytes(plain_password), password_hash.encode("utf-8"))
    except Exception:
        return False


def public_user(user: dict) -> dict:
    """Copia del usuario SIN el hash de contraseña, apta para guardar en la sesion."""
    return {key: value for key, value in (user or {}).items() if key != "password_hash"}


def _int_secret(key: str, default: int) -> int:
    try:
        value = int(safe_secret(key, default))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Limite de intentos de inicio de sesion
# ---------------------------------------------------------------------------

_failed_lock = threading.Lock()
_failed_logins: dict = {}  # correo -> {"count", "last", "locked_until"} (segundos epoch)
_dummy_hash_value = None


def _now() -> float:
    return time.time()


def reset_login_throttle() -> None:
    with _failed_lock:
        _failed_logins.clear()


def _login_limits() -> tuple:
    max_attempts = _int_secret("LOGIN_MAX_ATTEMPTS", DEFAULT_LOGIN_MAX_ATTEMPTS)
    lockout = _int_secret("LOGIN_LOCKOUT_MINUTES", DEFAULT_LOGIN_LOCKOUT_MINUTES) * 60
    return max_attempts, lockout


def _lockout_remaining(email: str, now: float) -> int:
    with _failed_lock:
        entry = _failed_logins.get(email)
        if entry and entry["locked_until"] > now:
            return math.ceil(entry["locked_until"] - now)
    return 0


def _register_failure(email: str, now: float) -> None:
    max_attempts, lockout = _login_limits()
    with _failed_lock:
        entry = _failed_logins.setdefault(email, {"count": 0, "last": now, "locked_until": 0.0})
        if now - entry["last"] > lockout and entry["locked_until"] <= now:
            entry["count"] = 0  # fallos antiguos ya no cuentan
        entry["count"] += 1
        entry["last"] = now
        if entry["count"] >= max_attempts:
            entry["locked_until"] = now + lockout
            entry["count"] = 0
            logger.warning("Inicio de sesion bloqueado temporalmente por intentos fallidos.")
        if len(_failed_logins) > _MAX_TRACKED_EMAILS:  # acota la memoria
            stale = [k for k, v in _failed_logins.items()
                     if v["locked_until"] <= now and now - v["last"] > lockout]
            for key in stale:
                del _failed_logins[key]


def _clear_failures(email: str) -> None:
    with _failed_lock:
        _failed_logins.pop(email, None)


def _dummy_hash() -> str:
    """Hash descartable: verificar contra el cuando la cuenta no existe iguala el
    tiempo de respuesta y evita distinguir cuentas por la latencia."""
    global _dummy_hash_value
    if _dummy_hash_value is None:
        _dummy_hash_value = hash_password(secrets.token_hex(16))
    return _dummy_hash_value


def login_user(storage, email: str, password: str):
    email = (email or "").strip().lower()
    now = _now()
    remaining = _lockout_remaining(email, now)
    if remaining:
        minutes = max(1, math.ceil(remaining / 60))
        return None, f"Demasiados intentos fallidos. Intenta de nuevo en {minutes} minuto(s)."

    user = storage.get_user_by_email(email)
    password_ok = verify_password(password or "", user.get("password_hash", "") if user else _dummy_hash())
    if not user or not password_ok:
        _register_failure(email, now)
        return None, GENERIC_LOGIN_ERROR
    if user.get("status") != "active":
        # Solo se revela tras acreditar la contraseña correcta.
        return None, DISABLED_ACCOUNT_ERROR

    _clear_failures(email)
    return public_user(user), None


# ---------------------------------------------------------------------------
# Sesion
# ---------------------------------------------------------------------------

def session_timeout_minutes() -> int:
    return _int_secret("SESSION_TIMEOUT_MINUTES", DEFAULT_SESSION_TIMEOUT_MINUTES)


def validate_session(storage, session_user: dict, login_at=None, now: datetime = None):
    """Revalida la sesion en cada recarga. Devuelve (usuario_actualizado, motivo).

    Si el usuario es None hay que cerrar la sesion y mostrar `motivo`. Leer el
    usuario de la base en cada recarga hace que un cambio de rol, o deshabilitar
    una cuenta, surta efecto de inmediato en vez de al proximo inicio de sesion."""
    now = now or datetime.now(timezone.utc)
    if login_at is not None and now - login_at > timedelta(minutes=session_timeout_minutes()):
        return None, "Tu sesion expiro. Inicia sesion de nuevo."
    fresh = storage.get_user_by_id((session_user or {}).get("id"))
    if not fresh:
        return None, "Tu cuenta ya no existe. Contacta a un administrador."
    if fresh.get("status") != "active":
        return None, "Tu cuenta fue deshabilitada. Contacta a un administrador."
    return public_user(fresh), None


# ---------------------------------------------------------------------------
# Cuenta maestra inicial y registro
# ---------------------------------------------------------------------------

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


def validate_registration(storage, full_name: str, email: str, password: str,
                          program: str, student_id: str):
    """Valida el formulario de registro. Devuelve (datos_preparados, error); los
    datos incluyen la contraseña ya hasheada, lista para `create_account`."""
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

    return {
        "full_name": full_name,
        "email": email,
        "password_hash": hash_password(password),
        "program": program,
        "student_id": student_id,
    }, None


def create_account(storage, prepared: dict, email_verified: bool = False):
    """Crea la cuenta. Devuelve (usuario, error).

    El rol profesor requiere correo en la lista blanca Y `email_verified=True`
    (el solicitante ya demostro ser dueño del correo)."""
    email = prepared["email"]
    if storage.get_user_by_email(email):  # puede haber cambiado desde la validacion
        return None, "Ya existe una cuenta registrada con ese correo."
    is_professor = email_verified and storage.is_email_whitelisted_professor(email)
    user = storage.create_user(
        full_name=prepared["full_name"],
        email=email,
        password_hash=prepared["password_hash"],
        role="profesor" if is_professor else "estudiante",
        program=prepared["program"],
        student_id=prepared["student_id"],
        status="active",
    )
    return user, None


def register_user(storage, full_name: str, email: str, password: str, program: str,
                  student_id: str, email_verified: bool = False):
    prepared, error = validate_registration(storage, full_name, email, password, program, student_id)
    if error:
        return None, error
    return create_account(storage, prepared, email_verified=email_verified)


# ---------------------------------------------------------------------------
# Verificacion de correo para el rol profesor
# ---------------------------------------------------------------------------

def needs_professor_verification(storage, email: str) -> bool:
    return bool(storage.is_email_whitelisted_professor((email or "").strip().lower()))


def _code_digest(salt: str, code: str) -> str:
    return hmac.new(salt.encode("utf-8"), code.encode("utf-8"), hashlib.sha256).hexdigest()


def new_verification_challenge(email: str, now: float = None):
    """Genera un codigo de 6 digitos. Devuelve (codigo, desafio). El desafio
    guarda solo un digest con sal: el codigo en claro nunca se almacena."""
    now = _now() if now is None else now
    code = f"{secrets.randbelow(10 ** 6):06d}"
    salt = secrets.token_hex(16)
    challenge = {
        "email": (email or "").strip().lower(),
        "salt": salt,
        "digest": _code_digest(salt, code),
        "expires_at": now + VERIFICATION_TTL_SECONDS,
        "attempts_left": VERIFICATION_MAX_ATTEMPTS,
    }
    return code, challenge


def check_verification_code(challenge: dict, code: str, now: float = None):
    """Comprueba el codigo. Devuelve (ok, mensaje, desafio_vigente). El desafio
    es None cuando ya no puede usarse (acierto, vencimiento o intentos agotados)."""
    now = _now() if now is None else now
    if not challenge:
        return False, "No hay una verificacion en curso.", None
    if now > challenge["expires_at"]:
        return False, "El codigo vencio. Solicita uno nuevo.", None
    candidate = (code or "").strip()
    if hmac.compare_digest(_code_digest(challenge["salt"], candidate), challenge["digest"]):
        return True, "Correo verificado.", None
    left = challenge["attempts_left"] - 1
    if left <= 0:
        return False, "Demasiados intentos incorrectos. Solicita un codigo nuevo.", None
    return False, f"Codigo incorrecto. Te quedan {left} intento(s).", {**challenge, "attempts_left": left}


def send_verification_email(email: str, code: str):
    """Envia el codigo al correo indicado. Devuelve (ok, mensaje); nunca lanza."""
    minutes = VERIFICATION_TTL_SECONDS // 60
    body = (
        f"Tu codigo de verificacion para el Laboratorio UNIMINUTO es: {code}\n\n"
        f"Vence en {minutes} minutos. Si no solicitaste este registro, ignora este mensaje."
    )
    return notifications.send_email_notification(
        "Codigo de verificacion - Laboratorio UNIMINUTO", body, [email]
    )


def validate_profile_update(storage, user_id: str, full_name: str, email: str,
                            program: str, student_id: str):
    """Valida correcciones administrativas sin alterar rol, estado o clave."""
    full_name = " ".join((full_name or "").strip().split())
    email = (email or "").strip().lower()
    program = " ".join((program or "").strip().split())
    student_id = (student_id or "").strip()
    if len(full_name.split()) < 2:
        return None, "Ingresa nombre y apellido."
    if not is_institutional_email(email):
        return None, "El correo debe ser institucional y terminar en .edu o .edu.co."
    if not student_id:
        return None, "El ID de estudiante es obligatorio."
    if not program:
        return None, "El programa académico o departamento es obligatorio."
    duplicate = storage.get_user_by_email(email)
    if duplicate and duplicate.get("id") != user_id:
        return None, "Ya existe otra cuenta con ese correo institucional."
    return {
        "full_name": full_name,
        "institutional_email": email,
        "program_or_department": program,
        "student_id": student_id,
    }, None
