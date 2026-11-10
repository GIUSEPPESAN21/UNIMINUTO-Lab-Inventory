# -*- coding: utf-8 -*-
"""Persistencia segura de sesión entre recargas de Streamlit.

El token solo contiene identificadores y tiempos. La cookie escrita desde
JavaScript no puede ser HttpOnly; se mitiga con expiración corta, SameSite=Lax,
Secure bajo HTTPS y revocación por ``session_epoch``.
"""

import base64
import hashlib
import hmac
import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from core import options
from core.config import safe_secret

COOKIE_NAME = "uniminuto_lab_session"
DEFAULT_IDLE_MINUTES = 30
DEFAULT_MAX_HOURS = 12
ROTATE_EVERY_SECONDS = 60
_REVOKED_LIMIT = 10000
_revoked_lock = threading.Lock()
_revoked: dict = {}          # sid -> None; conserva el orden de inserción (los más antiguos salen primero)


def _now(now=None) -> datetime:
    value = now or datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def needs_rotation(claims: dict, now=None) -> bool:
    """True si conviene renovar la marca de actividad del token (máx. 1/min)."""
    try:
        last = int((claims or {}).get("last") or 0)
    except (TypeError, ValueError):
        return True
    return int(_now(now).timestamp()) - last >= ROTATE_EVERY_SECONDS


def _integer(name: str, default: int, allow_zero: bool = False) -> int:
    try:
        value = int(safe_secret(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 or (allow_zero and value == 0) else default


def idle_minutes() -> int:
    return _integer("SESSION_IDLE_MINUTES", DEFAULT_IDLE_MINUTES)


def max_hours() -> int:
    return _integer("SESSION_MAX_HOURS", DEFAULT_MAX_HOURS)


def tab_close_grace_seconds() -> int:
    return _integer("SESSION_TAB_CLOSE_GRACE_SECONDS", 0, allow_zero=True)


# Latido opcional (S3-c). Vive en memoria del proceso: no escribe en el Excel
# ni en GitHub. Solo se consulta al RESTAURAR desde la cookie (pestaña nueva o
# F5); si la última señal es más vieja que la gracia, se asume pestaña cerrada.
_heartbeat_lock = threading.Lock()
_heartbeats: dict = {}


def beat(claims: dict, now=None) -> None:
    sid = str((claims or {}).get("sid") or "")
    if not sid:
        return
    with _heartbeat_lock:
        if len(_heartbeats) >= _REVOKED_LIMIT:
            _heartbeats.clear()
        _heartbeats[sid] = int(_now(now).timestamp())


def tab_closed(claims: dict, now=None) -> bool:
    grace = tab_close_grace_seconds()
    sid = str((claims or {}).get("sid") or "")
    if not grace or not sid:
        return False
    with _heartbeat_lock:
        last = _heartbeats.get(sid)
    # Sin registro (p. ej. el servidor se reinició) no se cierra por error.
    return last is not None and int(_now(now).timestamp()) - last > grace


def heartbeat_every():
    """Intervalo para `st.fragment(run_every=...)`; None si está apagado."""
    grace = tab_close_grace_seconds()
    return f"{max(2, grace // 3)}s" if grace else None


def session_secret() -> str:
    value = str(safe_secret("SESSION_SECRET", "") or "").strip()
    nfc_value = str(safe_secret("NFC_SECRET", "") or "").strip()
    return "" if not value or (nfc_value and hmac.compare_digest(value, nfc_value)) else value


def enabled() -> bool:
    return bool(session_secret())


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(body: str, secret: str) -> str:
    return _b64(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())


def token_version() -> int:
    """Versión global de los tokens de sesión (Secret ``SESSION_TOKEN_VERSION``, 1 por omisión).

    Subirla (p. ej. de 1 a 2) invalida TODOS los tokens persistentes ya emitidos, sin tocar el
    ``session_epoch`` de cada usuario; es el equivalente a un ``kid`` global. Solo se EXIGE (ver
    ``verify_token``) con la opción ``nfc_sesion_persistente`` activa: apagada, el claim ``ver`` se
    guarda igual en el token (no cambia su tamaño ni su firma de forma incompatible) pero nunca se
    revisa, así que el comportamiento de siempre no cambia."""
    try:
        return int(safe_secret("SESSION_TOKEN_VERSION", 1) or 1)
    except (TypeError, ValueError):
        return 1


def issue_token(user: dict, now=None, claims: dict = None) -> tuple:
    """Devuelve (token, claims). Sin SESSION_SECRET devuelve ("", {})."""
    secret = session_secret()
    if not secret or not user:
        return "", {}
    moment = _now(now)
    previous = claims or {}
    issued = int(previous.get("iat") or moment.timestamp())
    payload = {
        "uid": str(user.get("id") or ""),
        "epoch": int(user.get("session_epoch") or 0),
        "iat": issued,
        "last": int(moment.timestamp()),
        "exp": issued + max_hours() * 3600,
        "sid": str(previous.get("sid") or secrets.token_urlsafe(18)),
        "ver": token_version(),
    }
    body = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return f"{body}.{_sign(body, secret)}", payload


def decode_token(token: str) -> dict:
    secret = session_secret()
    if not secret or not token or token.count(".") != 1:
        raise ValueError("Sesión no disponible.")
    body, supplied = token.split(".", 1)
    expected = _sign(body, secret)
    if not hmac.compare_digest(supplied, expected):
        raise ValueError("Sesión inválida.")
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Sesión inválida.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Sesión inválida.")
    return payload


def revoke(token_or_claims, storage=None) -> None:
    """Cierra una sesión: la recuerda en memoria y, con ``storage``, también en la base
    hasta el vencimiento del token, así un reinicio de la app no la revive.

    Límite que se mantiene (documentado en el README): la cookie la escribe JavaScript,
    no puede ser HttpOnly; quien la haya copiado antes del cierre queda bloqueado por
    este registro, pero no por el navegador."""
    try:
        claims = decode_token(token_or_claims) if isinstance(token_or_claims, str) else token_or_claims
        sid = str((claims or {}).get("sid") or "")
    except ValueError:
        sid = ""
    if not sid:
        return
    with _revoked_lock:
        if len(_revoked) >= _REVOKED_LIMIT:
            # Antes se vaciaba todo y las sesiones cerradas volvían a ser válidas hasta su vencimiento;
            # ahora solo salen las más antiguas (la base conserva el resto, ver add_revoked_session).
            for old in list(_revoked)[: _REVOKED_LIMIT // 10]:
                del _revoked[old]
        _revoked[sid] = None
    if storage is not None:
        try:
            storage.add_revoked_session(sid, int((claims or {}).get("exp") or 0))
        except Exception:  # un fallo de E/S no debe impedir cerrar la sesión en memoria
            pass


def verify_token(token: str, storage, now=None) -> tuple:
    """Devuelve (usuario, claims, motivo). Siempre revalida estado y epoch."""
    if not session_secret():
        return None, None, "La persistencia de sesión no está configurada."
    try:
        claims = decode_token(token)
    except ValueError as exc:
        return None, None, str(exc)
    moment = int(_now(now).timestamp())
    with _revoked_lock:
        closed = claims.get("sid") in _revoked
    if not closed:
        try:
            closed = str(claims.get("sid") or "") in storage.get_revoked_sessions(moment)
        except Exception:  # sin registro legible, valen las demás comprobaciones
            closed = False
    if closed:
        return None, None, "La sesión fue cerrada."
    if moment > int(claims.get("exp") or 0):
        return None, None, "Tu sesión alcanzó su duración máxima."
    if moment - int(claims.get("last") or 0) > idle_minutes() * 60:
        return None, None, "Tu sesión se cerró por inactividad."
    user = storage.get_user_by_id(str(claims.get("uid") or ""))
    if not user or user.get("status") != "active":
        return None, None, "Tu cuenta no está disponible."
    if int(user.get("session_epoch") or 0) != int(claims.get("epoch") or 0):
        return None, None, "La sesión fue revocada. Inicia sesión de nuevo."
    # nfc_sesion_persistente: con la opción apagada esta comprobación no se hace (comportamiento de
    # siempre); activa, un token de una versión anterior a SESSION_TOKEN_VERSION queda revocado para
    # TODOS los usuarios de una sola vez, aunque su session_epoch individual no haya cambiado.
    if options.enabled(storage, options.NFC_SESSION_PERSISTENT) and int(claims.get("ver") or 1) != token_version():
        return None, None, "La sesión fue revocada (nueva versión). Inicia sesión de nuevo."
    from core.auth import public_user
    return public_user(user), claims, None


def cookie_from_context() -> str:
    try:
        import streamlit as st
        return str(st.context.cookies.get(COOKIE_NAME, "") or "")
    except Exception:
        return ""


_BRIDGE_DIR = Path(__file__).resolve().parent / "session_bridge"
_BRIDGE_KEY = "session_cookie_bridge"
RESTORE_DONE_KEY = "session_restore_done"   # esta pestaña ya intentó recuperar la sesión desde la cookie
# nfc_sesion_persistente: mientras el puente (JS) no contesta (cookie_from_browser() devuelve None), la
# pestaña espera en vez de decidir ya que hay que iniciar sesión (causa (c) del diagnóstico). Acotado para
# no quedar esperando para siempre si el puente nunca responde (navegador sin JS, componente roto, etc.).
RESTORE_WAIT_KEY = "session_restore_wait_tries"
RESTORE_WAIT_LIMIT = 2
RESTORE_WAIT_SECONDS = 0.3
_bridge = None


def cookie_from_browser():
    """Lee la cookie de sesión en el navegador (JavaScript) y la devuelve a Python.

    En Streamlit Community Cloud las cookies propias de la app no llegan al servidor:
    ``st.context.cookies`` queda vacío aunque el navegador tenga la cookie, así que cada
    pestaña nueva (cada toque de un chip NFC, cada F5) volvía a pedir el inicio de sesión.
    Este puente (``core/session_bridge/index.html``) la lee en el navegador. Devuelve
    ``None`` mientras el navegador no ha respondido (su respuesta provoca otra ejecución),
    ``""`` si no hay cookie o el puente no está disponible, y el token si existe."""
    global _bridge
    try:
        if _bridge is None:
            import streamlit.components.v1 as components
            _bridge = components.declare_component("session_bridge", path=str(_BRIDGE_DIR))
        value = _bridge(cookie_name=COOKIE_NAME, key=_BRIDGE_KEY, default=None)
    except Exception:  # sin puente se pide el inicio de sesión, como antes
        return ""
    if value is None:
        return None
    return str(value.get("token") or "") if isinstance(value, dict) else ""


_COMMAND_KEY = "session_cookie_command"


def queue_cookie(token: str = "", clear: bool = False, persistent: bool = False) -> None:
    """Programa escribir o borrar la cookie de sesión.

    La ESCRITURA se vuelve a emitir al inicio de cada ejecución hasta que una
    ejecución termina completa (``finish_cookie_command``). Si la ejecución que la
    emite sigue con ``st.switch_page`` o ``st.rerun``, Streamlit descarta lo que el
    navegador todavía no recibió: antes eso pasaba justo después de iniciar sesión
    desde un toque NFC (el toque lleva al conteo, al despacho o a la ruta), la
    cookie nunca llegaba y cada toque siguiente, que abre una pestaña nueva, volvía
    a pedir el inicio de sesión. El BORRADO se emite una sola vez, como antes: la
    revocación en el servidor ya invalida el token.

    ``persistent`` (nfc_sesion_persistente, apagada por omisión): si es True, la escritura incluye
    ``Max-Age`` para que la cookie sobreviva a cerrar la pestaña o el navegador, acotada por
    ``max_hours()``. Apagada, la cookie sigue siendo de sesión del navegador (sin ``Max-Age``), que
    es el comportamiento de siempre."""
    try:
        import streamlit as st
        st.session_state[_COMMAND_KEY] = {
            "token": token, "clear": bool(clear), "emitted": False, "persistent": bool(persistent),
        }
    except Exception:
        pass


def render_cookie_command() -> None:
    """Inicio de cada ejecución: emite la orden pendiente. Una escritura sigue pendiente hasta que una ejecución
    completa la confirme; un borrado sale una sola vez."""
    try:
        import streamlit as st
        command = st.session_state.get(_COMMAND_KEY)
    except Exception:
        return
    if not command:
        return
    if command.get("clear"):
        st.session_state.pop(_COMMAND_KEY, None)
    else:
        command["emitted"] = True
    _emit_cookie(command)


def finish_cookie_command() -> None:
    """Final de una ejecución completa: emite la orden programada durante ella (renovación, cambio de
    contraseña) y da por entregada la pendiente, porque lo emitido en esta ejecución ya quedó en pantalla."""
    try:
        import streamlit as st
        command = st.session_state.pop(_COMMAND_KEY, None)
    except Exception:
        return
    if command and not command.get("emitted"):
        _emit_cookie(command)


def _emit_cookie(command: dict) -> None:
    """Puente mínimo de cookie. No transmite el token a terceros."""
    try:
        import streamlit.components.v1 as components
    except Exception:
        return
    token = str(command.get("token") or "")
    clear = bool(command.get("clear"))
    name_js = json.dumps(COOKIE_NAME)
    value_js = json.dumps("" if clear else token)
    if clear:
        max_age = "; Max-Age=0"
    elif command.get("persistent"):
        # nfc_sesion_persistente: Max-Age acotado por max_hours(); la revalidación de exp/idle/epoch
        # en verify_token (en CADA carga) sigue siendo la que de verdad decide si el token aún sirve.
        max_age = f"; Max-Age={max_hours() * 3600}"
    else:
        max_age = ""
    html = f"""<script>
const name={name_js}; const value={value_js};
// Este script corre en un iframe about:srcdoc: el protocolo real es el de la página de la app.
const secure = window.parent.location.protocol === 'https:' ? '; Secure' : '';
window.parent.document.cookie = `${{name}}=${{encodeURIComponent(value)}}; Path=/; SameSite=Lax${{secure}}{max_age}`;
</script>"""
    components.html(html, width=0, height=0)


def audit(storage, event_type: str, user: dict, details: dict = None) -> None:
    """Login/logout/expiración; nunca registra el token ni cada renovación."""
    try:
        storage.add_trace_event({
            "event_type": event_type, "user_id": user.get("id", ""),
            "owner_id": user.get("id", ""), "actor_id": user.get("id", ""),
            "actor_name": user.get("full_name", ""),
            "actor_email": user.get("institutional_email", ""),
            "actor_role": user.get("role", ""),
            "details": json.dumps(details or {}, ensure_ascii=False),
        })
    except Exception:
        # La autenticación nunca debe quedar bloqueada por la auditoría remota.
        pass
