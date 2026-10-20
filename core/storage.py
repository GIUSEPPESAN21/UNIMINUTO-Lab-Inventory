# -*- coding: utf-8 -*-
"""
core/storage.py - Capa de base de datos: Excel local + sincronizacion automatica a GitHub.
Cache en memoria RAM, escritura atomica y publicacion sincrona y serializada en GitHub.
"""

import atexit
import base64
import io
import json
import logging
import os
import shutil
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Event, Lock, Thread

import pandas as pd
import requests
import streamlit as st

from core import barcode, excel_safe, fastxlsx
from core import reservations as reservation_rules
from core.config import safe_secret

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

EXCEL_PATH = "UNIMINUTO_LAB_DB.xlsx"

SHEET_COLUMNS = {
    "items": [
        "id", "name", "category", "description", "item_type", "parent_id",
        "unit", "quantity", "location", "min_stock_alert", "status",
        "created_by", "updated_at",
        # "1" si del producto (Lego, bloques, kits…) se retiran piezas sin inventario por pieza
        # (core/pieces.py). Las bases antiguas completan esta columna al cargar.
        "loose_pieces",
    ],
    "users": [
        "id", "full_name", "student_id", "institutional_email", "password_hash", "role",
        "program_or_department", "status", "created_at",
        # Restablecimiento de clave por el perfil maestro (core/auth.py):
        # `must_change_password` ("1" o vacio) y `password_changed_at` (ISO).
        # Las bases antiguas completan estas columnas automáticamente al cargar.
        "must_change_password", "password_changed_at", "session_epoch",
    ],
    "professors_whitelist": [
        "institutional_email",
    ],
    "loans": [
        "id", "item_id", "item_name", "parent_id", "quantity", "user_id",
        "user_name", "user_role", "checkout_at", "expected_return_at",
        "return_at", "status", "notes", "request_id",
        # Quién hizo el picking (despacho); vacío en salidas directas por Escanear.
        "operator_id", "operator_name",
    ],
    "reservations": [
        "id", "scope_type", "activity", "purpose", "attendees", "start_at", "end_at",
        "requester_id", "requester_name", "requester_email", "status", "reviewed_by",
        "reviewed_at", "review_notes", "email_notified", "email_error", "created_at", "updated_at",
    ],
    "service_requests": [
        "id", "request_type", "item_id", "item_name", "quantity", "service_name",
        "description", "needed_at", "requester_id", "requester_name", "requester_email",
        "status", "reviewed_by", "reviewed_at", "review_notes", "email_notified",
        "email_error", "created_at", "updated_at",
        # Pedido de varios productos: todas sus líneas comparten este código (vacío = pedido de un solo producto).
        "order_id",
    ],
    "item_history": [
        "id", "item_id", "timestamp", "type", "quantity_change",
        "actor_user_id", "details",
    ],
    # Configuracion del laboratorio (clave/valor), p. ej. el tamaño de etiqueta.
    "settings": [
        "key", "value", "updated_by", "updated_at",
    ],
    # Trazabilidad (core/traceability.py): ruta verificada, retiro, validacion
    # de comprobante y avance de servicios. `user_id` es el dueño de la
    # solicitud; `actor_*`, quien registro el evento; `details`, JSON.
    "trace_events": [
        "id", "event_type", "request_id", "seq", "item_id", "loan_id",
        "user_id", "owner_id", "actor_id", "actor_name", "actor_email",
        "actor_role", "receipt", "details", "created_at",
    ],
    # Registro de correos automaticos (core/email_events.py): que se envio, a
    # quien y si el servidor lo acepto. Se conservan los ultimos
    # NOTIFICATION_LOG_LIMIT para que la base no crezca sin limite.
    "notification_log": [
        "id", "event_type", "subject", "recipients", "recipient_count", "status",
        "error", "reference", "actor_email", "created_at",
    ],
    # Fotos de los objetos (core/photos.py): indice de las fotos de cada item.
    # `path` es la ruta del JPEG en el repositorio de datos (fotos/<codigo>/...),
    # `sha` el blob de git de ese archivo y `kind` registro | estado | inventario.
    "item_photos": [
        "id", "item_id", "path", "sha", "taken_by", "taken_at", "kind", "note",
    ],
}

NOTIFICATION_LOG_LIMIT = 500

_cached_dfs = None
_trace_request_index = {}
_excel_lock = Lock()
_load_lock = Lock()
_events_index_lock = Lock()
_events_index = {"version": None, "index": {}}   # ver LabStorage.get_events_index
_data_state_lock = Lock()
# `generation` sube solo cuando la base se (re)carga desde el archivo (inicio o descarga de
# GitHub); `version` sube además con cada escritura. Las cachés derivadas (core.dispatch)
# se invalidan por `version` y descartan lo aprendido al cambiar `generation`.
_data_state = {"version": 0, "generation": 0, "last_read_at": None, "last_poll_at": 0.0}


def _bump_data_version(read: bool = False, reload: bool = False) -> int:
    with _data_state_lock:
        _data_state["version"] += 1
        if reload:
            _data_state["generation"] += 1
        if read:
            _data_state["last_read_at"] = datetime.now(timezone.utc)
        return _data_state["version"]


def data_state() -> dict:
    with _data_state_lock:
        return dict(_data_state)


def _rebuild_trace_index(df: pd.DataFrame) -> dict:
    """Índice lógico request_id -> filas de `trace_events` (T2).

    Se asocia a la identidad y tamaño del DataFrame en caché; si la caché se
    reemplaza por otra vía, el índice se reconstruye solo en la siguiente consulta.
    """
    global _trace_request_index
    index = {}
    if "request_id" in df.columns:
        for position, request_id in zip(df.index, df["request_id"]):
            key = str(request_id or "")
            if key:
                index.setdefault(key, []).append(position)
    _trace_request_index = {"owner": id(df), "size": len(df), "rows": index}
    return index


def _trace_positions(df: pd.DataFrame, request_id: str) -> list:
    current = _trace_request_index
    if current.get("owner") != id(df) or current.get("size") != len(df):
        return _rebuild_trace_index(df).get(str(request_id), [])
    return current["rows"].get(str(request_id), [])


def _normalize_legacy_columns(dfs: dict) -> None:
    users = dfs.get("users")
    if users is not None and "session_epoch" in users:
        # astype(object): con pandas 3, apply sobre texto devuelve dtype `str`
        # estricto y luego rechaza el entero con el que se sube la época
        # (revocar sesiones fallaba con TypeError tras reiniciar la app).
        users["session_epoch"] = users["session_epoch"].apply(
            lambda value: 0 if str(value or "").strip() in {"", "nan", "None"} else value
        ).astype(object)
    events = dfs.get("trace_events")
    if events is None or events.empty:
        return
    missing_owner = events["owner_id"].astype(str).str.strip() == ""
    events.loc[missing_owner, "owner_id"] = events.loc[missing_owner, "user_id"]
    for request_id, positions in events.groupby("request_id", sort=False).groups.items():
        if not str(request_id or "").strip():
            continue
        used = set()
        for position in positions:
            try:
                value = int(float(events.at[position, "seq"] or 0))
            except (TypeError, ValueError):
                value = 0
            if value > 0 and value not in used:
                used.add(value)
            else:
                value = 1
                while value in used:
                    value += 1
                events.at[position, "seq"] = value
                used.add(value)


# ---------------------------------------------------------------------------
# Sincronizacion con GitHub
# ---------------------------------------------------------------------------
# Reglas de diseño (ver README, "Integridad de datos"):
#
# 1. El Excel local se escribe de forma ATOMICA (archivo temporal + os.replace):
#    nadie, ni el hilo que sube a GitHub, puede leer un archivo a medio escribir.
# 2. Cada escritura marca la base como "sucia". Al salir de la seccion critica
#    (`_db_write`) se publica la ULTIMA version local en GitHub, de forma
#    SINCRONA y serializada (`_push_lock`): cuando la operacion termina, el
#    cambio ya esta publicado (o el fallo quedo registrado y visible). Varias
#    escrituras simultaneas se agrupan: solo la primera sube, la ultima version.
# 3. Nunca se pisa una version remota que esta app no haya visto: se recuerda el
#    SHA de la ultima descarga/subida y, si GitHub tiene otro (alguien edito el
#    archivo, hay otra instancia, o nunca se logro descargar), el push se
#    rechaza y se marca un CONFLICTO que el maestro resuelve desde la interfaz.
#
# El estado se guarda aparte para que la UI (hilo principal de cada rerun)
# muestre de forma confiable si la ultima sincronizacion funciono o no (ver
# LabStorage.get_sync_status()).

_sync_status_lock = Lock()
_sync_status = {
    "configured": False,   # hay GITHUB_TOKEN + GITHUB_REPO en los Secrets
    "ok": None,             # None = todavia no se intento ninguna operacion
    "message": "",
    "last_op": None,        # "pull" | "push"
    "last_at": None,
    "repo": "",
    "db_path": "",
    "conflict": False,      # True = GitHub tiene una version que esta app no conoce
}

_push_lock = Lock()
_sync_state_lock = Lock()
# dirty: hay cambios sin publicar en GitHub. file_dirty (solo SYNC_MODE=deferred): la
# caché en memoria tiene cambios que aún no se escribieron en el archivo Excel local.
_sync_state = {"dirty": False, "remote_sha": None, "file_dirty": False}

_TRANSIENT_STATUS = (429, 500, 502, 503, 504)


def _reset_sync_state() -> None:
    """Restablece el estado de sincronizacion (uso en pruebas)."""
    with _sync_status_lock:
        _sync_status.update(
            configured=False, ok=None, message="", last_op=None, last_at=None,
            repo="", db_path="", conflict=False,
        )
    with _sync_state_lock:
        _sync_state.update(dirty=False, remote_sha=None, file_dirty=False)
    _stop_flusher()


def _set_sync_status(configured: bool, ok, message: str, op: str = None,
                     conflict: bool = False) -> None:
    with _sync_status_lock:
        _sync_status["configured"] = configured
        _sync_status["ok"] = ok
        _sync_status["message"] = message
        _sync_status["last_op"] = op
        _sync_status["last_at"] = datetime.now(timezone.utc).isoformat()
        _sync_status["repo"] = safe_secret("GITHUB_REPO", "")
        _sync_status["db_path"] = safe_secret("GITHUB_DB_PATH", EXCEL_PATH)
        _sync_status["conflict"] = conflict


def get_sync_status() -> dict:
    with _sync_status_lock:
        status = dict(_sync_status)
    with _sync_state_lock:
        pending = bool(_sync_state["dirty"] or _sync_state["file_dirty"])
    status.update(mode=sync_mode(), pending=pending)
    return status


def _mark_dirty() -> None:
    with _sync_state_lock:
        _sync_state["dirty"] = True


def _clear_dirty() -> None:
    with _sync_state_lock:
        _sync_state["dirty"] = False


def _take_dirty() -> bool:
    """Devuelve si habia cambios sin publicar y limpia la marca."""
    with _sync_state_lock:
        was_dirty = _sync_state["dirty"]
        _sync_state["dirty"] = False
        return was_dirty


def _get_remote_sha():
    with _sync_state_lock:
        return _sync_state["remote_sha"]


def _set_remote_sha(sha) -> None:
    with _sync_state_lock:
        _sync_state["remote_sha"] = sha or None


def _ui_toast(message: str, icon: str = None) -> None:
    try:
        st.toast(message, icon=icon)
    except Exception:  # fuera de una sesion de Streamlit (pruebas, scripts)
        pass


def _ui_warning(message: str) -> None:
    try:
        st.warning(message)
    except Exception:
        pass


def _github_headers() -> dict:
    token = safe_secret("GITHUB_TOKEN", "")
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }


def _github_api_url() -> str:
    repo = safe_secret("GITHUB_REPO", "")
    db_path = safe_secret("GITHUB_DB_PATH", EXCEL_PATH)
    return f"https://api.github.com/repos/{repo}/contents/{db_path}"


def _is_github_configured() -> bool:
    token = safe_secret("GITHUB_TOKEN", "")
    repo = safe_secret("GITHUB_REPO", "")
    if not token or not repo:
        logger.warning(
            "GitHub sync desactivado. Agrega GITHUB_TOKEN y GITHUB_REPO en los Secrets de Streamlit."
        )
        _set_sync_status(
            configured=False, ok=None,
            message="Falta GITHUB_TOKEN y/o GITHUB_REPO en los Secrets de Streamlit.",
        )
        return False
    return True


_PUT_RACE_RETRIES = 2            # reintentos de un PUT que GitHub rechazó con 409/422 sin que el archivo cambiara
_MAX_RATE_WAIT_SECONDS = 10     # más que esto no se espera dentro de una petición: se informa y se reintenta después


def _header(headers, name: str) -> str:
    """Valor de una cabecera sin distinguir mayúsculas ("" si no está)."""
    wanted = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == wanted:
            return str(value)
    return ""


def _rate_limit_seconds(status: int, headers, body: str):
    """Segundos que pide GitHub esperar si la respuesta es un límite de tasa; si no, ``None``.

    Un 403 con ``Retry-After`` / ``X-RateLimit-Remaining: 0`` o un 429 son límite de tasa,
    no un token inválido (un 403 sin esas señales sí es de permisos)."""
    if status not in (403, 429):
        return None
    retry_after = _header(headers, "Retry-After")
    remaining = _header(headers, "X-RateLimit-Remaining")
    if not (status == 429 or retry_after or remaining == "0" or "rate limit" in str(body or "").lower()):
        return None
    try:
        return max(0.0, float(retry_after))
    except ValueError:
        pass
    try:
        return max(0.0, float(_header(headers, "X-RateLimit-Reset")) - time.time())
    except ValueError:
        return 2.0


def _rate_limit_wait(resp):
    return _rate_limit_seconds(getattr(resp, "status_code", 0), getattr(resp, "headers", None),
                               getattr(resp, "text", ""))


def _github_request(method: str, url: str, attempts: int = 3, session=None, **kwargs):
    """Llamada a la API de GitHub con reintentos ante fallos transitorios de red,
    respuestas 429/5xx y límites de tasa (esperas de 1 s y 2 s, o lo que pida
    ``Retry-After`` hasta ``_MAX_RATE_WAIT_SECONDS``). Los demas codigos HTTP se
    devuelven tal cual para que el llamador los interprete. `session` (opcional)
    es un requests.Session u objeto con `.request` (las fotos lo usan en pruebas)."""
    last_exc = None
    client = session or requests
    for attempt in range(attempts):
        delay = 2 ** attempt
        try:
            resp = client.request(method, url, headers=_github_headers(), **kwargs)
        except requests.RequestException as exc:
            last_exc = exc
        else:
            limited = _rate_limit_wait(resp)
            if limited is not None:
                if limited > _MAX_RATE_WAIT_SECONDS:
                    return resp          # esperar tanto dentro de una petición no sirve: se informa
                delay = max(delay, limited)
            if (resp.status_code not in _TRANSIENT_STATUS and limited is None) or attempt == attempts - 1:
                return resp
        if attempt < attempts - 1:
            time.sleep(delay)
    raise last_exc


def _atomic_write_bytes(path: str, data: bytes) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


_MIN_REMOTE_CHECK_SECONDS = 10


def _github_remote_fingerprint():
    """SHA remoto del Excel sin descargarlo: lista el directorio que lo contiene.

    Devuelve None si no se pudo consultar (la app sigue con su copia local).
    """
    if not _is_github_configured():
        return None
    repo = safe_secret("GITHUB_REPO", "")
    db_path = str(safe_secret("GITHUB_DB_PATH", EXCEL_PATH) or EXCEL_PATH).strip("/")
    folder, _, name = db_path.rpartition("/")
    url = f"https://api.github.com/repos/{repo}/contents/{folder}" if folder else \
        f"https://api.github.com/repos/{repo}/contents"
    try:
        resp = _github_request("GET", url, attempts=1, timeout=15)
        if resp.status_code != 200:
            return None
        entries = resp.json()
        if not isinstance(entries, list):
            return None
        match = next((entry for entry in entries if entry.get("name") == name), None)
        return (match or {}).get("sha") or None
    except Exception as exc:  # red caída: no es un error del usuario
        logger.info(f"GitHub fingerprint no disponible: {exc}")
        return None


class RemoteUnavailableError(RuntimeError):
    """GitHub está configurado pero no se pudo obtener la base y no hay copia local.

    Arrancar con un libro vacío en ese caso serviría datos vacíos y dejaría al
    administrador a un clic de sobrescribir la base real (conflicto → «conservar la
    de esta app»). Es preferible detenerse y reintentar."""


def _validate_workbook_bytes(data: bytes) -> None:
    """Lanza ``ValueError`` si ``data`` no es un libro .xlsx con hojas de la base.

    Se usa antes de reemplazar el archivo local con lo descargado: una descarga
    cortada o un archivo que no es la base nunca debe pisar la copia buena."""
    try:
        workbook = pd.ExcelFile(io.BytesIO(data), engine="openpyxl")
        known = [sheet for sheet in workbook.sheet_names if sheet in SHEET_COLUMNS]
        for sheet in known:
            workbook.parse(sheet, nrows=0)
    except Exception as exc:
        raise ValueError("el archivo descargado no es un libro Excel válido") from exc
    if not known:
        raise ValueError("el archivo descargado no tiene ninguna hoja de la base de datos")


def _backup_local_copy() -> None:
    """Guarda la última copia local buena como ``<base>.bak`` antes de reemplazarla."""
    try:
        if os.path.exists(EXCEL_PATH):
            shutil.copy2(EXCEL_PATH, f"{EXCEL_PATH}.bak")
    except OSError as exc:      # una copia de seguridad que falla no debe impedir sincronizar
        logger.warning(f"No se pudo guardar la copia {EXCEL_PATH}.bak: {exc}")


def _github_pull() -> bool:
    """Descarga la base desde GitHub y reemplaza el archivo local.

    Devuelve True solo si el archivo local fue reemplazado. Recuerda el SHA
    descargado: es la referencia para detectar cambios externos mas adelante."""
    global _cached_dfs
    if not _is_github_configured():
        return False
    try:
        url = _github_api_url()
        logger.info(f"GitHub pull -> {url}")
        resp = _github_request("GET", url, timeout=30)

        if resp.status_code == 404:
            logger.info(f"{EXCEL_PATH} no existe en GitHub todavia. Se creara al primer guardado.")
            _set_remote_sha(None)
            _set_sync_status(
                configured=True, ok=True,
                message="Conectado. La base de datos aun no existe en GitHub; se creara al primer guardado.",
                op="pull",
            )
            return False

        if resp.status_code != 200:
            msg = _describe_response(resp)
            logger.error(f"GitHub pull fallo: HTTP {resp.status_code} - {resp.text[:300]}")
            _ui_warning(f"No se pudo sincronizar con GitHub: {msg}")
            _set_sync_status(configured=True, ok=False, message=msg, op="pull")
            return False

        data = resp.json()
        if data.get("encoding") == "base64" and data.get("content"):
            file_bytes = base64.b64decode(data["content"])
        elif data.get("download_url"):
            dl_resp = requests.get(data["download_url"], timeout=60)
            dl_resp.raise_for_status()
            file_bytes = dl_resp.content
        else:
            logger.warning("GitHub pull: respuesta inesperada, sin contenido.")
            _set_sync_status(
                configured=True, ok=False,
                message="GitHub respondio sin contenido descargable.", op="pull",
            )
            return False

        try:
            _validate_workbook_bytes(file_bytes)
        except ValueError as exc:
            logger.error(f"GitHub pull: descarga descartada: {exc}")
            message = f"La descarga de GitHub se descartó porque {exc}. Se conserva la copia local."
            _ui_warning(message)
            _set_sync_status(configured=True, ok=False, message=message, op="pull")
            return False

        _backup_local_copy()
        _atomic_write_bytes(EXCEL_PATH, file_bytes)
        _cached_dfs = None
        _set_remote_sha(data.get("sha"))
        _clear_dirty()
        with _sync_state_lock:
            _sync_state["file_dirty"] = False   # el archivo recién descargado manda
        logger.info(f"{EXCEL_PATH} descargado desde GitHub ({len(file_bytes) / 1024:.1f} KB).")
        _set_sync_status(
            configured=True, ok=True,
            message=f"Ultima descarga OK ({len(file_bytes) / 1024:.1f} KB).", op="pull",
        )
        return True

    except Exception as e:
        logger.error(f"GitHub pull error: {e}")
        _ui_warning(f"No se pudo descargar la base de datos desde GitHub: {e}")
        _set_sync_status(configured=True, ok=False, message=str(e), op="pull")
        return False


def _describe_github_error(status_code: int, body: str, headers=None) -> str:
    """Traduce los codigos de error mas comunes de la API de GitHub a un
    mensaje accionable (en vez de solo el HTTP crudo)."""
    wait = _rate_limit_seconds(status_code, headers, body)
    if wait is not None:
        return (
            "GitHub limitó temporalmente las solicitudes (límite de tasa); no es un problema del token. "
            + (f"Podrá reintentar en unos {int(wait) + 1} s. " if wait else "")
            + "Los cambios quedan pendientes y se vuelven a enviar solos."
        )
    if status_code == 404:
        repo = safe_secret("GITHUB_REPO", "")
        return (
            f"El repositorio '{repo}' no existe o el token no tiene acceso a el. "
            "Verifica GITHUB_REPO en los Secrets de Streamlit (Settings -> Secrets)."
        )
    if status_code == 409:
        return (
            "GitHub rechazó la publicación por un commit simultáneo en el repositorio (HTTP 409) aunque "
            "el archivo no cambió. Los cambios quedan pendientes y se vuelven a enviar solos."
        )
    if status_code in (401, 403):
        return (
            "El GITHUB_TOKEN no es valido o no tiene permiso de escritura "
            "('Contents: Read and write') sobre el repositorio configurado."
        )
    return f"HTTP {status_code} - {body[:300]}"


def _describe_response(resp) -> str:
    """Mensaje accionable para una respuesta de GitHub que no fue exitosa."""
    return _describe_github_error(resp.status_code, resp.text, getattr(resp, "headers", None))


_CONFLICT_MESSAGE = (
    "La base de datos en GitHub es distinta de la que esta app descargo (alguien la edito "
    "fuera de la app, hay otra instancia escribiendo, o aun no se logro descargarla). "
    "Para no pisar esos datos, los cambios recientes quedaron SOLO en este contenedor. "
    "El administrador debe elegir que version conservar."
)


def _github_push(force: bool = False) -> bool:
    """Publica el Excel local en GitHub. Nunca lanza; devuelve si se publico.

    Sin `force`, rechaza el push (y marca conflicto) si GitHub tiene una version
    que esta app no conoce. Con `force=True` la version local gana."""
    if not _is_github_configured():
        return False
    try:
        url = _github_api_url()
        _persist_file_if_dirty()   # SYNC_MODE=deferred: el archivo debe estar al día antes de subirlo

        with open(EXCEL_PATH, "rb") as f:
            raw_bytes = f.read()
        content_b64 = base64.b64encode(raw_bytes).decode("utf-8")

        get_resp = _github_request("GET", url, timeout=15)
        if get_resp.status_code == 200:
            remote_sha = get_resp.json().get("sha", "")
        elif get_resp.status_code == 404:
            remote_sha = ""
        else:
            msg = _describe_response(get_resp)
            logger.error(f"GitHub push: no se pudo leer el SHA: HTTP {get_resp.status_code}")
            _ui_warning(f"Error de sincronizacion con GitHub: {msg}")
            _set_sync_status(configured=True, ok=False, message=msg, op="push")
            return False

        if remote_sha and not force and remote_sha != _get_remote_sha():
            logger.error("GitHub push rechazado: la version remota no es la que esta app conoce.")
            _set_sync_status(
                configured=True, ok=False, message=_CONFLICT_MESSAGE, op="push", conflict=True
            )
            return False

        commit_msg = f"Auto-sync {EXCEL_PATH} [{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC]"
        payload = {"message": commit_msg, "content": content_b64}
        if remote_sha:
            payload["sha"] = remote_sha

        put_resp = _github_request("PUT", url, json=payload, timeout=60)
        # Un 409/422 no siempre es un conflicto: GitHub también lo devuelve cuando dos commits
        # llegan a la vez al mismo repositorio (p. ej. una foto subida justo ahora). Solo es un
        # conflicto real si el archivo remoto cambió; si sigue igual, se reintenta con espera.
        remote_changed = False
        for retry in range(1, _PUT_RACE_RETRIES + 1):
            if put_resp.status_code not in (409, 422):
                break
            time.sleep(0.5 * retry)
            fresh = _github_request("GET", url, timeout=15)
            if fresh.status_code == 200:
                fresh_sha = fresh.json().get("sha", "")
            elif fresh.status_code == 404:
                fresh_sha = ""
            else:
                break                      # no se pudo comprobar: falla ahora y se reintenta después
            if fresh_sha != remote_sha:
                remote_changed = True      # el archivo remoto cambió de verdad: conflicto real
                break
            put_resp = _github_request("PUT", url, json=payload, timeout=60)
        if put_resp.status_code in (200, 201):
            new_sha = ((put_resp.json() or {}).get("content") or {}).get("sha") or ""
            if not new_sha:
                again = _github_request("GET", url, timeout=15)
                new_sha = again.json().get("sha", "") if again.status_code == 200 else ""
            _set_remote_sha(new_sha)
            logger.info("Base de datos sincronizada con GitHub correctamente.")
            _ui_toast("Datos guardados y sincronizados con GitHub", icon="✅")
            _set_sync_status(configured=True, ok=True, message="Ultima sincronizacion OK.", op="push")
            return True

        if put_resp.status_code in (409, 422) and not force and remote_changed:
            # Alguien publico entre nuestra lectura del SHA y el PUT.
            _set_sync_status(
                configured=True, ok=False, message=_CONFLICT_MESSAGE, op="push", conflict=True
            )
            return False

        msg = _describe_response(put_resp)
        logger.error(f"GitHub push fallo: HTTP {put_resp.status_code} - {put_resp.text[:500]}")
        _ui_warning(f"Error de sincronizacion con GitHub: {msg}")
        _set_sync_status(configured=True, ok=False, message=msg, op="push")
        return False

    except FileNotFoundError:
        logger.error(f"No se encontro el archivo local {EXCEL_PATH} para subir a GitHub.")
        _set_sync_status(
            configured=True, ok=False,
            message=f"No se encontro el archivo local {EXCEL_PATH} para subir.", op="push",
        )
        return False
    except Exception as e:
        logger.error(f"GitHub push error inesperado: {e}")
        _ui_warning(f"Error inesperado al sincronizar con GitHub: {e}")
        _set_sync_status(configured=True, ok=False, message=str(e), op="push")
        return False


def _publish_pending() -> None:
    """Publica la ultima version local si hay cambios sin publicar. Nunca lanza.

    Es seguro llamarla desde varios hilos: se serializa y, si otro hilo ya
    publico una version mas reciente, no hace nada. Si el push falla, la marca
    de "cambios pendientes" se conserva para el siguiente intento."""
    if not _is_github_configured():
        return
    with _push_lock:
        if not _take_dirty():
            return
        if not _github_push():
            _mark_dirty()


def _flush_pending_push() -> None:
    """Fin de cada escritura: publica ya (modo normal) o deja el trabajo al hilo
    de fondo (``SYNC_MODE=deferred``)."""
    if sync_mode() == "deferred":
        _schedule_flush()
        return
    _publish_pending()


# ---------------------------------------------------------------------------
# SYNC_MODE=deferred (opcional, apagado por defecto)
# ---------------------------------------------------------------------------
# En el modo normal cada escritura serializa TODO el libro y sube el archivo a
# GitHub antes de devolver el control (con varias personas escribiendo, cada una
# espera su turno de ambas cosas). En el modo diferido la escritura solo cambia la
# caché en memoria (que es de donde leen todas las sesiones) y un hilo de fondo
# escribe el archivo y publica UNA vez por ráfaga. Riesgo asumido: si el proceso
# muere antes del siguiente vaciado se pierden los cambios de ese intervalo
# (SYNC_DEFER_SECONDS, 1 s por omisión, más lo que tarde GitHub); un `atexit`
# intenta vaciar al cerrar normalmente. Los fallos y conflictos se ven en el mismo
# aviso de sincronización; `get_sync_status()["pending"]` indica cambios sin publicar.

DEFAULT_DEFER_SECONDS = 1.0
DEFAULT_RETRY_SECONDS = 15.0

_flusher_lock = Lock()
_flusher = {"thread": None, "stop": None, "atexit": False}
_flush_event = Event()


def sync_mode() -> str:
    """``deferred`` solo si SYNC_MODE lo pide; cualquier otro valor es el modo normal."""
    return "deferred" if str(safe_secret("SYNC_MODE", "") or "").strip().lower() == "deferred" else "sync"


def _seconds_secret(name: str, default: float) -> float:
    try:
        return max(0.0, float(safe_secret(name, default)))
    except (TypeError, ValueError):
        return default


def _persist_file_if_dirty() -> None:
    """Escribe en el archivo lo que la caché tiene y el archivo aún no (modo diferido)."""
    with _sync_state_lock:
        needed = _sync_state["file_dirty"]
        _sync_state["file_dirty"] = False
    if not needed:
        return
    snapshot = _cached_dfs   # los escritores reemplazan el diccionario completo: la referencia es estable
    if snapshot is None:
        return
    try:
        _serialize_to_disk(snapshot)
    except Exception:
        with _sync_state_lock:
            _sync_state["file_dirty"] = True
        raise


def flush_now() -> bool:
    """Escribe el archivo y publica en GitHub ahora, de forma síncrona (cierre, pruebas).
    Devuelve True si no quedó nada pendiente."""
    try:
        _persist_file_if_dirty()
    except Exception as exc:
        logger.error(f"No se pudo escribir el archivo local: {exc}")
        return False
    _publish_pending()
    with _sync_state_lock:
        return not (_sync_state["dirty"] or _sync_state["file_dirty"])


def _flusher_loop(stop: Event) -> None:
    while not stop.is_set():
        _flush_event.wait()
        if stop.is_set():
            return
        time.sleep(_seconds_secret("SYNC_DEFER_SECONDS", DEFAULT_DEFER_SECONDS))  # junta la ráfaga
        _flush_event.clear()
        try:
            clean = flush_now()
        except Exception as exc:  # el hilo de fondo nunca debe morir
            logger.error(f"Vaciado diferido fallido: {exc}")
            clean = False
        if not clean and not get_sync_status().get("conflict"):
            if stop.wait(_seconds_secret("SYNC_RETRY_SECONDS", DEFAULT_RETRY_SECONDS)):
                return
            _flush_event.set()      # reintenta (un conflicto espera a la persona que administra)


def _schedule_flush() -> None:
    with _flusher_lock:
        thread = _flusher["thread"]
        if thread is None or not thread.is_alive():
            stop = Event()
            thread = Thread(target=_flusher_loop, args=(stop,), name="lab-db-flusher", daemon=True)
            _flusher.update(thread=thread, stop=stop)
            thread.start()
        if not _flusher["atexit"]:
            atexit.register(flush_now)
            _flusher["atexit"] = True
    _flush_event.set()


def _stop_flusher() -> None:
    """Detiene el hilo de fondo (pruebas y reinicios del estado)."""
    with _flusher_lock:
        stop, _flusher["thread"], _flusher["stop"] = _flusher["stop"], None, None
    if stop is not None:
        stop.set()
        _flush_event.set()
    _flush_event.clear()


@contextmanager
def _db_write():
    """Seccion critica de escritura.

    Serializa el ciclo leer-modificar-escribir del Excel y, al salir (con o sin
    error), publica en GitHub la ultima version local, FUERA del lock de datos
    para no bloquear a los demas usuarios mientras dura la llamada de red."""
    try:
        with _excel_lock:
            yield
    finally:
        _flush_pending_push()


# ---------------------------------------------------------------------------
# Helpers internos de bajo nivel
# ---------------------------------------------------------------------------

def _load_cache() -> dict:
    """Carga (una sola vez) las hojas del Excel a memoria."""
    global _cached_dfs
    if _cached_dfs is not None:
        return _cached_dfs
    with _load_lock:                  # un solo lector parsea; los demás esperan y reutilizan
        if _cached_dfs is not None:
            return _cached_dfs
        try:
            xls = pd.ExcelFile(EXCEL_PATH, engine="openpyxl")
            dfs = {}
            for sheet, cols in SHEET_COLUMNS.items():
                if sheet in xls.sheet_names:
                    # keep_default_na=False + fillna: las celdas vacias llegan como "" (no
                    # como NaN, que rompia .strip() al editar items tras recargar el Excel)
                    # y un texto legitimo como "NA" o "None" ya no se convierte en nulo.
                    # astype(object): desde pandas 3, dtype=str crea columnas de texto
                    # estrictas que rechazan numeros (editar la cantidad de un item tras
                    # reiniciar fallaba con TypeError); object conserva el comportamiento
                    # de pandas 2 (cada celda leida sigue siendo texto).
                    df = xls.parse(sheet, dtype=str, keep_default_na=False).fillna("").astype(object)
                    for col in cols:
                        if col not in df.columns:
                            df[col] = ""
                    dfs[sheet] = df[cols]
                else:
                    dfs[sheet] = pd.DataFrame(columns=cols)
        except FileNotFoundError:
            dfs = {sheet: pd.DataFrame(columns=cols) for sheet, cols in SHEET_COLUMNS.items()}
        _normalize_legacy_columns(dfs)
        if _cached_dfs is not None:   # una escritura publicó una versión nueva mientras se parseaba: gana la nueva
            return _cached_dfs
        _cached_dfs = dfs
        _rebuild_trace_index(dfs["trace_events"])
        _bump_data_version(read=True, reload=True)
        return _cached_dfs


def _read_excel(*sheets: str) -> dict:
    """Copias de las hojas pedidas (todas si no se indica ninguna). Los lectores
    piden solo lo que usan para no copiar la base completa en cada consulta."""
    cache = _load_cache()
    names = sheets or tuple(cache)
    return {name: cache[name].copy() for name in names}


def _workbook_headers(path: str) -> dict:
    """``{hoja: [columnas]}`` leyendo solo la primera fila de cada hoja.

    Revisar el esquema al arrancar no debe leer todo el libro (con una base grande eran 2.5–5 s). Para los
    libros que escribe la app se lee solo el principio del XML de cada hoja; si el libro fue guardado por
    Excel (textos compartidos) se usa la lectura de openpyxl."""
    try:
        return _fast_headers(path)
    except Exception:
        import openpyxl

        book = openpyxl.load_workbook(path, read_only=True)
        try:
            return {sheet.title: [str(value) for value in next(sheet.iter_rows(max_row=1, values_only=True), ())
                                  if value is not None] for sheet in book.worksheets}
        finally:
            book.close()


def _fast_headers(path: str) -> dict:
    import re
    import zipfile
    from xml.sax.saxutils import unescape

    with zipfile.ZipFile(path) as archive:
        workbook = archive.read("xl/workbook.xml").decode("utf-8")
        rels = archive.read("xl/_rels/workbook.xml.rels").decode("utf-8")
        targets = {m.group(1): m.group(2) for m in re.finditer(r'<Relationship\b[^>]*?Id="([^"]+)"[^>]*?Target="([^"]+)"', rels)}
        targets.update({m.group(2): m.group(1) for m in re.finditer(r'<Relationship\b[^>]*?Target="([^"]+)"[^>]*?Id="([^"]+)"', rels)})
        headers = {}
        for sheet in re.finditer(r"<sheet\b[^>]*>", workbook):
            tag = sheet.group(0)
            name = re.search(r'\bname="([^"]*)"', tag).group(1)
            rid = re.search(r'\br:id="([^"]+)"', tag).group(1)
            target = targets[rid].lstrip("/")
            member = target if target.startswith("xl/") else f"xl/{target}"
            with archive.open(member) as handle:
                head = handle.read(65536).decode("utf-8", "ignore")
            row = re.search(r"<row\b[^>]*>(.*?)</row>", head, re.S)
            if row is None:
                headers[unescape(name, {"&quot;": '"'})] = []
                continue
            cells = re.findall(r"<c\b([^>]*)>(.*?)</c>", row.group(1), re.S)
            if any('t="s"' in attrs for attrs, _ in cells):
                raise ValueError("textos compartidos: lectura completa")
            headers[unescape(name, {"&quot;": '"'})] = [
                unescape("".join(re.findall(r"<t\b[^>]*>(.*?)</t>", body, re.S))) for _, body in cells]
        return headers


def _peek(*sheets: str) -> dict:
    """Las hojas pedidas **sin copiarlas**, solo para leer.

    ``_read_excel`` copia cada hoja (necesario para quien la va a modificar); con miles de filas, una
    simple búsqueda por código copiaba todo el inventario. Quien use ``_peek`` no puede modificar el
    resultado: los escritores reemplazan las hojas de la caché, nunca las mutan, así que una lectura en
    curso siempre ve una versión coherente."""
    cache = _load_cache()
    return {name: cache[name] for name in (sheets or tuple(cache))}


_write_times_ms: list = []          # duración de las últimas escrituras del libro (salud de la base)


def write_stats() -> dict:
    """Duración (ms) de la última escritura del libro y percentil 95 de las recientes."""
    recent = sorted(_write_times_ms)
    if not recent:
        return {"last_ms": None, "p95_ms": None, "count": 0}
    return {"last_ms": _write_times_ms[-1], "p95_ms": recent[min(len(recent) - 1, int(len(recent) * 0.95))],
            "count": len(recent)}


def _serialize_to_disk(dfs: dict) -> None:
    """Serializa las hojas y reemplaza el archivo de forma atomica."""
    started = time.perf_counter()
    try:
        _serialize_workbook(dfs)
    finally:
        _write_times_ms.append(round((time.perf_counter() - started) * 1000))
        del _write_times_ms[:-50]


def _serialize_workbook(dfs: dict) -> None:
    buffer = io.BytesIO()
    try:
        fastxlsx.write_workbook(buffer, dfs)         # ~15 veces más rápido que pandas con una base grande
    except Exception:
        logger.exception("El escritor rápido falló; se usa pandas para guardar el libro.")
        buffer = io.BytesIO()
        excel_safe.write_frames(buffer, dfs)         # sin fórmulas accidentales ni caracteres de control
    try:
        _atomic_write_bytes(EXCEL_PATH, buffer.getvalue())
    except Exception:
        try:
            os.remove(f"{EXCEL_PATH}.tmp")
        except OSError:
            pass
        raise


def _write_excel(dfs: dict) -> None:
    """Escribe el Excel de forma atomica: o queda la version anterior completa
    o la nueva completa, nunca un archivo a medias.

    Con ``SYNC_MODE=deferred`` (apagado por defecto) la escritura al archivo se
    difiere: solo se actualiza la caché en memoria, que es la que leen todas las
    sesiones, y el archivo se escribe junto con la publicación en GitHub
    (``flush_now``). Si el archivo todavía no existe se escribe en el acto."""
    global _cached_dfs
    deferred = sync_mode() == "deferred" and os.path.exists(EXCEL_PATH)
    if not deferred:
        _serialize_to_disk(dfs)
    _cached_dfs = {k: v.copy() for k, v in dfs.items()}
    _rebuild_trace_index(_cached_dfs["trace_events"])
    if deferred:
        with _sync_state_lock:
            _sync_state["file_dirty"] = True


def _write_and_sync(dfs: dict) -> None:
    """Guarda en disco y marca la base como pendiente de publicar. La
    publicacion en GitHub ocurre al cerrar la seccion `_db_write`."""
    _write_excel(dfs)
    _mark_dirty()
    _bump_data_version(read=True)


def _int(value, default: int = 0) -> int:
    """Entero tolerante a celdas vacías o con texto ("2", "2.0", "")."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _new_id() -> str:
    return uuid.uuid4().hex[:20]


def _now_str() -> str:
    return datetime.now(timezone.utc).isoformat()


_NAN_TEXT = frozenset(("nan", "None", "NaT"))


def _clean_nan(d: dict) -> dict:
    for k, v in d.items():
        if v is None or (v in _NAN_TEXT if v.__class__ is str else str(v) in _NAN_TEXT):
            d[k] = None
    return d


def _item_from_dict(data: dict) -> dict:
    item = dict(data)
    for num_col in ("quantity", "min_stock_alert"):
        try:
            v = item.get(num_col)
            item[num_col] = int(float(v)) if v not in (None, "", "nan", "None") else 0
        except Exception:
            item[num_col] = 0
    return _clean_nan(item)


def _row_to_item(row: pd.Series) -> dict:
    return _item_from_dict(row.to_dict())


def _user_from_dict(data: dict) -> dict:
    record = _clean_nan(dict(data))
    try:
        record["session_epoch"] = int(float(record.get("session_epoch") or 0))
    except (TypeError, ValueError):
        record["session_epoch"] = 0
    return record


def _row_to_user(row: pd.Series) -> dict:
    return _user_from_dict(row.to_dict())


def _loan_from_dict(data: dict) -> dict:
    loan = dict(data)
    try:
        loan["quantity"] = int(float(loan.get("quantity") or 0))
    except Exception:
        loan["quantity"] = 0

    for date_field in ("checkout_at", "expected_return_at", "return_at"):
        raw = loan.get(date_field)
        if raw and str(raw).strip() and str(raw) not in ("nan", "None"):
            try:
                ts = datetime.fromisoformat(str(raw))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                loan[date_field] = ts
            except Exception:
                loan[date_field] = None
        else:
            loan[date_field] = None
    return _clean_nan(loan)


def _row_to_loan(row: pd.Series) -> dict:
    return _loan_from_dict(row.to_dict())


def _parse_record_dates(record: dict, fields: tuple) -> dict:
    for field in fields:
        raw = record.get(field)
        if raw and str(raw).strip() not in ("", "nan", "None"):
            try:
                value = datetime.fromisoformat(str(raw))
                record[field] = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                record[field] = None
        else:
            record[field] = None
    return record


def _reservation_from_dict(data: dict) -> dict:
    record = _clean_nan(dict(data))
    try:
        record["attendees"] = int(float(record.get("attendees") or 0))
    except (TypeError, ValueError):
        record["attendees"] = 0
    record["email_notified"] = str(record.get("email_notified") or "").lower() in ("true", "1")
    return _parse_record_dates(
        record, ("start_at", "end_at", "reviewed_at", "created_at", "updated_at")
    )


def _row_to_reservation(row: pd.Series) -> dict:
    return _reservation_from_dict(row.to_dict())


def _service_request_from_dict(data: dict) -> dict:
    record = _clean_nan(dict(data))
    try:
        record["quantity"] = int(float(record.get("quantity") or 0))
    except (TypeError, ValueError):
        record["quantity"] = 0
    record["email_notified"] = str(record.get("email_notified") or "").lower() in ("true", "1")
    return _parse_record_dates(
        record, ("needed_at", "reviewed_at", "created_at", "updated_at")
    )


def _row_to_service_request(row: pd.Series) -> dict:
    return _service_request_from_dict(row.to_dict())


VALID_ITEM_TYPES = ("master", "child", "standalone", "location")
LOCATION_TYPE = "location"  # estanteria, piso, mesa o zona (ver core/places.py)


def _validate_location_data(parent_id: str, df_items: pd.DataFrame, custom_id: str, is_new: bool) -> None:
    """Reglas de una ubicacion: su codigo es de ubicacion (nunca el de un
    contenedor o producto) y, si esta dentro de otra, esa otra es una ubicacion
    compatible (un piso en su estanteria). La jerarquia se fija al crearla."""
    if not is_new:
        return
    barcode.validate_location_code(custom_id)
    if not parent_id:
        return
    if parent_id == custom_id:
        raise ValueError("Una ubicacion no puede estar dentro de si misma.")
    parent_rows = df_items[df_items["id"] == parent_id]
    if parent_rows.empty:
        raise ValueError(f"La ubicacion '{parent_id}' no existe.")
    if parent_rows.iloc[0]["item_type"] != LOCATION_TYPE:
        raise ValueError(f"'{parent_id}' no es una ubicacion (estanteria, piso, mesa o zona).")
    if parent_rows.iloc[0]["status"] == "retired":
        raise ValueError(f"La ubicacion '{parent_id}' esta dada de baja.")
    problem = barcode.location_parent_problem(custom_id, parent_id)
    if problem:
        raise ValueError(problem)


def _validate_item_data(data: dict, df_items: pd.DataFrame, custom_id: str, is_new: bool = False) -> None:
    item_type = data.get("item_type", "standalone")
    parent_id = str(data.get("parent_id") or "").strip()
    if parent_id.lower() in ("nan", "none"):  # celdas nulas heredadas de Excel
        parent_id = ""

    if item_type == LOCATION_TYPE:
        _validate_location_data(parent_id, df_items, custom_id, is_new)
        return

    if is_new:
        barcode.validate_code_format(custom_id)

    if item_type not in VALID_ITEM_TYPES:
        raise ValueError(f"Tipo de item invalido: '{item_type}'.")

    if item_type == "child":
        if not parent_id:
            raise ValueError("Un Contenedor de Característica debe tener un Contenedor Principal asignado.")
        if parent_id == custom_id:
            raise ValueError("Un item no puede ser su propio contenedor.")
        parent_rows = df_items[df_items["id"] == parent_id]
        if parent_rows.empty:
            raise ValueError(f"El Contenedor Principal '{parent_id}' no existe.")
        if parent_rows.iloc[0]["item_type"] != "master":
            raise ValueError(f"'{parent_id}' no es un Contenedor Principal valido.")
    else:
        if parent_id:
            raise ValueError("Solo un Contenedor de Característica puede tener un Contenedor Principal asignado.")


# Errores que reintentar no puede arreglar: datos invalidos (ValueError) o
# fallos de programacion. Se propagan de inmediato con su mensaje real.
_NON_RETRYABLE = (ValueError, KeyError, TypeError, AttributeError, PermissionError)


def _authorize(actor, capability: str, message: str = "No tienes permiso para esta acción.") -> None:
    """Autorización de la capa de datos: con ``actor`` exige la capacidad.

    ``actor=None`` identifica a un llamador interno de confianza (arranque,
    migraciones, scripts); las vistas y la lógica de dominio siempre pasan el
    actor, y un test recorre el código para comprobarlo.
    """
    if actor is None:
        return
    from core import permissions
    permissions.require_cap(actor, capability, message)


def with_retry(func):
    """Reintenta (3 veces, con espera exponencial) las operaciones que fallan
    por causas transitorias (E/S, red). Nombre heredado: `firestore_retry`."""
    def wrapper(*args, **kwargs):
        max_retries = 3
        delay = 1
        last_exception = None
        for attempt in range(max_retries):
            try:
                return func(*args, **kwargs)
            except _NON_RETRYABLE:
                # No es transitorio: reintentar no lo va a arreglar. Propagar
                # de inmediato para que la UI muestre el mensaje real en vez
                # de agotar reintentos.
                raise
            except Exception as e:
                last_exception = e
                logger.warning(f"Intento {attempt + 1} fallo en {func.__name__}: {e}. Reintentando...")
                if attempt < max_retries - 1:
                    time.sleep(delay)
                    delay *= 2
        logger.error(f"Todos los reintentos fallaron para {func.__name__}.")
        raise last_exception
    return wrapper


firestore_retry = with_retry  # alias retrocompatible


def _cascade_ids(df_items: pd.DataFrame, item_id: str) -> list:
    """El item y, si es un Contenedor Principal, todos los items que contiene
    (activos o dados de baja)."""
    ids = [item_id]
    rows = df_items[df_items["id"] == item_id]
    if not rows.empty and rows.iloc[0]["item_type"] == "master":
        ids.extend(df_items[df_items["parent_id"] == item_id]["id"].tolist())
    elif not rows.empty and rows.iloc[0]["item_type"] == LOCATION_TYPE:
        # Una ubicacion arrastra las ubicaciones que contiene (los pisos de una
        # estanteria...), a cualquier profundidad. Los productos no se tocan.
        pending = [item_id]
        while pending:
            current = pending.pop()
            inner = df_items[(df_items["parent_id"] == current) & (df_items["item_type"] == LOCATION_TYPE)]
            for child_id in inner["id"].tolist():
                if child_id not in ids:
                    ids.append(child_id)
                    pending.append(child_id)
    return ids


def _purge_item_records(dfs: dict, ids: list, actor_email: str = "") -> dict:
    """Borra DEFINITIVAMENTE todo rastro de los items `ids` en `dfs` (se modifica
    en lugar): el propio item, su historial y sus prestamos ya devueltos.

    Las solicitudes de producto pendientes o aprobadas que apuntaban a esos
    items se cancelan (no se borran: pertenecen a otros usuarios) para que, si el
    codigo se reutiliza, no queden enlazadas a un producto distinto. El llamador
    debe haber comprobado antes que no hay prestamos abiertos."""
    df_items, df_hist = dfs["items"], dfs["item_history"]
    df_loans, df_req = dfs["loans"], dfs["service_requests"]

    open_requests = df_req["item_id"].isin(ids) & df_req["status"].isin(("pending", "approved"))
    counts = {
        "items": int(df_items["id"].isin(ids).sum()),
        "history": int(df_hist["item_id"].isin(ids).sum()),
        "loans": int(df_loans["item_id"].isin(ids).sum()),
        "requests_cancelled": int(open_requests.sum()),
    }

    dfs["items"] = df_items[~df_items["id"].isin(ids)].reset_index(drop=True)
    dfs["item_history"] = df_hist[~df_hist["item_id"].isin(ids)].reset_index(drop=True)
    dfs["loans"] = df_loans[~df_loans["item_id"].isin(ids)].reset_index(drop=True)
    if "item_photos" in dfs:
        # Las fotos salen del indice (si el codigo se reutiliza, el objeto nuevo no
        # hereda fotos ajenas). Los JPEG siguen en el repositorio de datos.
        df_photos = dfs["item_photos"]
        counts["photos"] = int(df_photos["item_id"].isin(ids).sum())
        dfs["item_photos"] = df_photos[~df_photos["item_id"].isin(ids)].reset_index(drop=True)

    if open_requests.any():
        now = _now_str()
        for column, value in {
            "status": "cancelled", "reviewed_by": actor_email, "reviewed_at": now,
            "review_notes": "Producto eliminado del inventario.", "updated_at": now,
        }.items():
            df_req.loc[open_requests, column] = value
        dfs["service_requests"] = df_req
    return counts


def _quantity(value) -> int:
    try:
        return int(float(value)) if value not in (None, "", "nan", "None") else 0
    except (TypeError, ValueError):
        return 0


def _available_from(dfs: dict, item_id: str):
    """Disponibilidad calculada sobre `dfs` (items + loans). None si el item no existe."""
    items = dfs["items"]
    rows = items[items["id"] == item_id]
    if rows.empty:
        return None
    total = _quantity(rows["quantity"].iloc[0])
    loans = dfs["loans"]
    open_loans = loans.loc[(loans["item_id"] == item_id) & (loans["status"] == "out"), "quantity"]
    return max(total - sum(_quantity(value) for value in open_loans), 0)


# ---------------------------------------------------------------------------
# Clase principal
# ---------------------------------------------------------------------------

class LabStorage:
    """Capa de base de datos sobre Excel local con sincronizacion automatica a GitHub."""

    def __init__(self):
        self._ensure_excel_exists()

    def _ensure_excel_exists(self):
        _github_pull()
        if not os.path.exists(EXCEL_PATH):
            status = get_sync_status()
            if status["configured"] and status["ok"] is False and status["last_op"] == "pull":
                # Sin copia local y sin poder leer GitHub (no es un 404): no se inventa una base vacía.
                raise RemoteUnavailableError(
                    "No se pudo descargar la base de datos desde GitHub y no hay una copia local. "
                    f"Para no mostrar datos vacíos la app se detuvo. Motivo: {status['message']} "
                    "Reintenta en un momento; si persiste, revisa GITHUB_TOKEN y GITHUB_REPO en los Secrets."
                )
            logger.info(f"Creando archivo Excel nuevo: {EXCEL_PATH}")
            with _db_write():
                dfs = {sheet: pd.DataFrame(columns=cols) for sheet, cols in SHEET_COLUMNS.items()}
                _write_and_sync(dfs)
        else:
            try:
                headers = _workbook_headers(EXCEL_PATH)        # solo la primera fila de cada hoja
                needs_migration = bool(set(SHEET_COLUMNS) - set(headers)) or any(
                    set(columns) - set(headers.get(sheet, ())) for sheet, columns in SHEET_COLUMNS.items())
                if needs_migration:
                    with _excel_lock:
                        _write_excel(_read_excel())
            except Exception:
                # La app sigue con lo que haya, pero el motivo queda en el registro.
                logger.exception("No se pudo revisar o migrar el libro local %s", EXCEL_PATH)

    def is_github_sync_active(self) -> bool:
        return _is_github_configured()

    def get_sync_status(self) -> dict:
        """Estado de la última sincronización con GitHub."""
        return get_sync_status()

    def get_data_state(self) -> dict:
        return data_state()

    def refresh_data(self, force_remote: bool = False) -> dict:
        """Botón «Actualizar» (A1): no toca ``st.session_state``.

        En un solo proceso la caché en memoria ya refleja todas las escrituras
        de todas las sesiones, así que solo se marca la lectura como reciente.
        Con ``force_remote`` se compara además la huella remota (limitada a una
        consulta cada ``_MIN_REMOTE_CHECK_SECONDS`` para todas las sesiones).
        """
        if force_remote:
            self.poll_remote(_MIN_REMOTE_CHECK_SECONDS)
        _load_cache()
        with _data_state_lock:
            _data_state["last_read_at"] = datetime.now(timezone.utc)
        return data_state()

    def poll_remote(self, interval_seconds: int = 60) -> bool:
        """Recarga solo si la base cambió fuera de este proceso (A1).

        Una consulta barata por intervalo para TODAS las sesiones: se pide el
        listado del directorio (sin contenido) y se compara el SHA del archivo.
        Nunca descarga encima de cambios locales aún sin publicar.
        """
        if not _is_github_configured():
            return False
        interval = max(1, int(interval_seconds or 60))
        moment = time.monotonic()
        with _data_state_lock:
            last = float(_data_state["last_poll_at"] or 0)
            if last and moment - last < interval:
                return False
            _data_state["last_poll_at"] = moment
        with _sync_state_lock:
            dirty = _sync_state["dirty"]
        if dirty:
            return False
        remote = _github_remote_fingerprint()
        if not remote or remote == _get_remote_sha():
            return False
        with _excel_lock:
            with _sync_state_lock:
                if _sync_state["dirty"]:
                    return False
            if not _github_pull():          # invalida la caché al reemplazar el archivo
                return False
            _load_cache()
        return True

    def flush_pending(self) -> bool:
        """Vacía ya lo diferido (SYNC_MODE=deferred): archivo local y GitHub."""
        return flush_now()

    def retry_sync(self) -> bool:
        """Reintenta publicar ahora la version local (sin forzar). Devuelve si
        la ultima sincronizacion quedo sin errores."""
        _mark_dirty()
        flush_now()      # síncrono también en SYNC_MODE=deferred: la persona espera el resultado
        return get_sync_status()["ok"] is not False

    def resolve_sync_conflict(self, keep: str, actor: dict = None):
        """Resuelve un conflicto de sincronizacion. Devuelve (ok, mensaje).

        keep="remote": descarta lo local y adopta la version de GitHub.
        keep="local":  publica la version local sobre la de GitHub (la remota
                       anterior sigue disponible en el historial de commits).

        Con ``actor`` exige la capacidad ``resolve_sync`` y deja constancia en la
        trazabilidad de quién eligió qué versión."""
        if keep not in ("remote", "local"):
            raise ValueError("keep debe ser 'remote' o 'local'.")
        _authorize(actor, "resolve_sync", "Solo un administrador resuelve conflictos de sincronización.")
        if keep == "remote":
            with _excel_lock:
                replaced = _github_pull()
            if not replaced:
                return False, get_sync_status()["message"] or "No hay una version remota que descargar."
            result = (True, "Se cargo la version de GitHub. Los cambios locales sin publicar se descartaron.")
        else:
            with _push_lock:
                published = _github_push(force=True)
                if published:
                    _clear_dirty()
            if not published:
                return False, get_sync_status()["message"]
            result = (True, "Se publico la version local en GitHub.")
        if actor is not None:
            try:      # la constancia no debe deshacer una resolución que ya se hizo
                self.add_trace_event({
                    "event_type": "sync_conflict_resolved", "user_id": actor.get("id", ""),
                    "actor_id": actor.get("id", ""), "actor_name": actor.get("full_name", ""),
                    "actor_email": actor.get("institutional_email", ""), "actor_role": actor.get("role", ""),
                    "details": json.dumps({"kept": keep}),
                })
            except Exception:
                logger.exception("No se pudo registrar la resolución del conflicto de sincronización")
        return result

    # ------------------------------------------------------------------
    # ITEMS (productos, contenedores maestros e items hijos)
    # ------------------------------------------------------------------

    @firestore_retry
    def save_item(self, data: dict, custom_id: str, is_new: bool = False, actor_email: str = "",
                  details: str = None, actor: dict = None):
        _authorize(actor, "edit_inventory", "No tienes permiso para crear o editar ítems.")
        with _db_write():
            dfs = _read_excel()
            df_items = dfs["items"]
            df_hist = dfs["item_history"]

            idx = df_items.index[df_items["id"] == custom_id].tolist()
            existing = df_items.loc[idx[0]] if idx else None

            if is_new and existing is not None:
                if existing["status"] != "retired":
                    raise ValueError("Ya existe un item con ese codigo.")
                # Un codigo dado de baja queda libre: se borra su rastro (para que
                # no herede historial ni prestamos) y se crea como item nuevo.
                _purge_item_records(dfs, _cascade_ids(df_items, custom_id), actor_email)
                df_items, df_hist = dfs["items"], dfs["item_history"]
                idx, existing = [], None

            item_type = existing["item_type"] if existing is not None else data.get("item_type", "standalone")
            parent_id = existing["parent_id"] if existing is not None else (data.get("parent_id", "") or "")
            _validate_item_data(
                {"item_type": item_type, "parent_id": parent_id}, df_items, custom_id, is_new=(existing is None)
            )

            old_quantity = int(float(existing["quantity"])) if existing is not None and str(existing["quantity"]).strip() not in ("", "nan") else 0
            new_quantity = int(data.get("quantity", 0) or 0)
            min_stock_alert = data.get("min_stock_alert", 0)
            if item_type == LOCATION_TYPE:
                new_quantity = min_stock_alert = 0  # una ubicacion no tiene stock: nunca se presta
            quantity_delta = new_quantity - old_quantity if existing is not None else new_quantity

            history_type = "Alta" if is_new else "Ajuste"
            details = details or ("Item creado en el sistema." if is_new else "Item actualizado manualmente.")

            row = {
                "id": custom_id,
                "name": data.get("name", ""),
                "category": data.get("category", ""),
                "description": data.get("description", ""),
                "item_type": item_type,
                "parent_id": parent_id,
                "unit": data.get("unit", "unidad"),
                "quantity": new_quantity,
                "location": data.get("location", ""),
                "min_stock_alert": min_stock_alert,
                "status": data.get("status", "active"),
                "created_by": data.get("created_by", actor_email),
                "updated_at": _now_str(),
                # Quien edita sin tocar la casilla (otros formularios, importaciones) no la borra.
                "loose_pieces": ("1" if str(data["loose_pieces"]).strip().lower() in ("1", "true", "si", "sí", "x")
                                 else "") if "loose_pieces" in data
                else (_clean_nan(existing.to_dict()).get("loose_pieces") or "" if existing is not None else ""),
            }

            if idx:
                for k, v in row.items():
                    if k == "created_by" and not is_new:
                        continue
                    df_items.at[idx[0], k] = v
            else:
                df_items = pd.concat([df_items, pd.DataFrame([row])], ignore_index=True)

            hist_row = {
                "id": _new_id(),
                "item_id": custom_id,
                "timestamp": _now_str(),
                "type": history_type,
                "quantity_change": quantity_delta,
                "actor_user_id": actor_email,
                "details": details,
            }
            df_hist = pd.concat([df_hist, pd.DataFrame([hist_row])], ignore_index=True)

            dfs["items"] = df_items
            dfs["item_history"] = df_hist
            _write_and_sync(dfs)
            logger.info(f"Item guardado/actualizado: {custom_id}")

    def bulk_upsert_items(self, rows: list, actor_email: str = "", actor: dict = None) -> dict:
        """Crea/actualiza muchos items en UNA sola lectura+escritura+sync
        (evita decenas de commits a GitHub en una importacion masiva)."""
        _authorize(actor, "edit_inventory", "No tienes permiso para importar ítems.")
        created, updated, errors = [], [], []
        with _db_write():
            dfs = _read_excel()
            df_items = dfs["items"]
            df_hist = dfs["item_history"]

            for raw in rows:
                custom_id = str(raw.get("id", "")).strip()
                name = str(raw.get("name", "")).strip()
                if not custom_id or not name:
                    errors.append(f"Fila omitida: id o nombre vacios ({raw}).")
                    continue

                item_type = str(raw.get("item_type", "standalone")).strip() or "standalone"
                parent_id = str(raw.get("parent_id", "") or "").strip()
                existing_rows = df_items[df_items["id"] == custom_id]
                was_retired = not existing_rows.empty and existing_rows.iloc[0]["status"] == "retired"
                is_new = existing_rows.empty or was_retired
                try:
                    _validate_item_data(
                        {"item_type": item_type, "parent_id": parent_id}, df_items, custom_id, is_new=is_new
                    )
                except ValueError as e:
                    errors.append(f"'{custom_id}': {e}")
                    continue
                if was_retired:
                    # Codigo liberado por una baja anterior: se reutiliza como item nuevo.
                    _purge_item_records(dfs, _cascade_ids(df_items, custom_id), actor_email)
                    df_items, df_hist = dfs["items"], dfs["item_history"]
                no_stock = item_type == LOCATION_TYPE  # una ubicacion no tiene stock
                row = {
                    "id": custom_id,
                    "name": name,
                    "category": str(raw.get("category", "") or ""),
                    "description": str(raw.get("description", "") or ""),
                    "item_type": item_type,
                    "parent_id": parent_id,
                    "unit": str(raw.get("unit", "unidad") or "unidad"),
                    "quantity": 0 if no_stock else int(float(raw.get("quantity", 0) or 0)),
                    "location": str(raw.get("location", "") or ""),
                    "min_stock_alert": 0 if no_stock else int(float(raw.get("min_stock_alert", 0) or 0)),
                    "status": "active",
                    "created_by": actor_email,
                    "updated_at": _now_str(),
                }

                idx = df_items.index[df_items["id"] == custom_id].tolist()
                if idx:
                    for k, v in row.items():
                        if k == "created_by":
                            continue
                        df_items.at[idx[0], k] = v
                    updated.append(custom_id)
                else:
                    df_items = pd.concat([df_items, pd.DataFrame([row])], ignore_index=True)
                    created.append(custom_id)

                hist_row = {
                    "id": _new_id(), "item_id": custom_id, "timestamp": _now_str(),
                    "type": "Alta" if is_new else "Ajuste", "quantity_change": row["quantity"],
                    "actor_user_id": actor_email, "details": "Importacion masiva (CSV).",
                }
                df_hist = pd.concat([df_hist, pd.DataFrame([hist_row])], ignore_index=True)

            if created or updated:
                dfs["items"] = df_items
                dfs["item_history"] = df_hist
                _write_and_sync(dfs)

        return {"created": created, "updated": updated, "errors": errors}

    def retire_item(self, item_id: str, actor_email: str = "", actor: dict = None):
        _authorize(actor, "edit_inventory", "No tienes permiso para dar de baja ítems.")
        with _db_write():
            dfs = _read_excel()
            df_items = dfs["items"]
            df_loans = dfs["loans"]

            rows = df_items[df_items["id"] == item_id]
            if rows.empty:
                return False, "El item no existe."
            item_type = rows.iloc[0]["item_type"]

            ids_to_retire = [item_id]
            if item_type == "master":
                children_ids = df_items[df_items["parent_id"] == item_id]["id"].tolist()
                ids_to_retire.extend(children_ids)
            elif item_type == LOCATION_TYPE:
                ids_to_retire = _cascade_ids(df_items, item_id)  # con las ubicaciones que contiene

            open_for_these = df_loans[df_loans["item_id"].isin(ids_to_retire) & (df_loans["status"] == "out")]
            if not open_for_these.empty:
                return False, "No se puede dar de baja: hay prestamos abiertos de este item o de items dentro de el."

            now = _now_str()
            hist_rows = []
            for iid in ids_to_retire:
                idx = df_items.index[df_items["id"] == iid].tolist()
                if idx:
                    df_items.at[idx[0], "status"] = "retired"
                    df_items.at[idx[0], "updated_at"] = now
                hist_rows.append({
                    "id": _new_id(), "item_id": iid, "timestamp": now,
                    "type": "Baja", "quantity_change": 0, "actor_user_id": actor_email,
                    "details": "Item dado de baja." if iid == item_id else f"Baja en cascada (contenedor {item_id}).",
                })

            dfs["items"] = df_items
            dfs["item_history"] = pd.concat([dfs["item_history"], pd.DataFrame(hist_rows)], ignore_index=True)
            _write_and_sync(dfs)
            return True, "Item dado de baja correctamente."

    def delete_item(self, item_id: str, actor_email: str = "", actor: dict = None):
        """Elimina DEFINITIVAMENTE un item de la base de datos. Devuelve (ok, mensaje).

        A diferencia de `retire_item` (baja logica que conserva la fila), aqui
        desaparece todo: el item, su historial y sus prestamos devueltos. Si es un
        Contenedor Principal tambien se eliminan los items que contiene. Las
        solicitudes pendientes o aprobadas del producto se cancelan. Se bloquea si
        hay prestamos abiertos. El codigo queda libre para volver a usarse."""
        _authorize(actor, "edit_inventory", "No tienes permiso para eliminar ítems.")
        item_id = (item_id or "").strip()
        with _db_write():
            dfs = _read_excel()
            df_items = dfs["items"]
            rows = df_items[df_items["id"] == item_id]
            if rows.empty:
                return False, "El item no existe."

            ids = _cascade_ids(df_items, item_id)
            df_loans = dfs["loans"]
            if not df_loans[df_loans["item_id"].isin(ids) & (df_loans["status"] == "out")].empty:
                return False, (
                    "No se puede eliminar: hay prestamos abiertos de este item o de items dentro de el."
                )

            name = rows.iloc[0]["name"]
            counts = _purge_item_records(dfs, ids, actor_email)
            _write_and_sync(dfs)
            logger.info(f"Item eliminado definitivamente: {item_id} ({counts}) por {actor_email or 'sistema'}")

        extra = f" y {len(ids) - 1} item(s) que contenia" if len(ids) > 1 else ""
        return True, f"'{name}'{extra} se elimino definitivamente de la base de datos."

    @firestore_retry
    def get_delete_impact(self, item_id: str) -> dict:
        """Resumen de lo que borraria `delete_item` (para pedir confirmacion)."""
        dfs = _peek("items", "loans", "service_requests")
        df_items, df_loans, df_req = dfs["items"], dfs["loans"], dfs["service_requests"]
        if df_items[df_items["id"] == item_id].empty:
            return {"exists": False, "contained_items": 0, "open_loans": 0,
                    "closed_loans": 0, "open_requests": 0}
        ids = _cascade_ids(df_items, item_id)
        loans = df_loans[df_loans["item_id"].isin(ids)]
        impact = {
            "exists": True,
            "contained_items": len(ids) - 1,
            "open_loans": int((loans["status"] == "out").sum()),
            "closed_loans": int((loans["status"] != "out").sum()),
            "open_requests": int(
                (df_req["item_id"].isin(ids) & df_req["status"].isin(("pending", "approved"))).sum()
            ),
        }
        if df_items[df_items["id"] == item_id].iloc[0]["item_type"] == LOCATION_TYPE:
            impact["item_type"] = LOCATION_TYPE  # lo contenido son ubicaciones, no productos
        return impact

    @firestore_retry
    def item_code_in_use(self, code: str) -> bool:
        """True si `code` pertenece a un item que NO esta dado de baja. Un codigo
        de un item dado de baja se considera libre y puede volver a registrarse."""
        item = self.get_item((code or "").strip())
        return bool(item) and item.get("status") != "retired"

    @firestore_retry
    def get_item(self, item_id: str):
        dfs = _peek("items")
        rows = dfs["items"][dfs["items"]["id"] == item_id]
        if rows.empty:
            return None
        return _row_to_item(rows.iloc[0])

    @firestore_retry
    def get_all_items(self, include_retired: bool = False) -> list:
        dfs = _peek("items")
        df = dfs["items"]
        items = [_item_from_dict(record) for record in df.to_dict("records")]
        if not include_retired:
            items = [i for i in items if i.get("status") != "retired"]
        return sorted(items, key=lambda x: (x.get("name") or "").lower())

    @firestore_retry
    def get_children(self, parent_id: str) -> list:
        dfs = _peek("items")
        df = dfs["items"]
        rows = df[(df["parent_id"] == parent_id) & (df["status"] != "retired")]
        items = [_item_from_dict(record) for record in rows.to_dict("records")]
        return sorted(items, key=lambda x: (x.get("name") or "").lower())

    @firestore_retry
    def get_all_masters(self) -> list:
        dfs = _peek("items")
        df = dfs["items"]
        rows = df[(df["item_type"] == "master") & (df["status"] != "retired")]
        items = [_item_from_dict(record) for record in rows.to_dict("records")]
        return sorted(items, key=lambda x: (x.get("name") or "").lower())

    @firestore_retry
    def get_available_quantity(self, item_id: str) -> int:
        available = _available_from(_peek("items", "loans"), item_id)
        return 0 if available is None else available

    @firestore_retry
    def get_availability_map(self) -> dict:
        """Disponibilidad de TODOS los items en una sola pasada: {id: disponible}.
        Evita una lectura de la base por cada item al listar el catalogo."""
        dfs = _peek("items", "loans")
        loans = dfs["loans"]
        borrowed = {}
        open_loans = loans.loc[loans["status"] == "out", ["item_id", "quantity"]]
        for item_id, quantity in zip(open_loans["item_id"], open_loans["quantity"]):
            borrowed[item_id] = borrowed.get(item_id, 0) + _quantity(quantity)
        items = dfs["items"]
        return {item_id: max(_quantity(total) - borrowed.get(item_id, 0), 0)
                for item_id, total in zip(items["id"], items["quantity"])}

    # ------------------------------------------------------------------
    # USUARIOS
    # ------------------------------------------------------------------

    @firestore_retry
    def get_user_by_email(self, email: str):
        dfs = _peek("users")
        df = dfs["users"]
        rows = df[df["institutional_email"] == email.lower().strip()]
        if rows.empty:
            return None
        return _row_to_user(rows.iloc[0])

    @firestore_retry
    def get_user_by_id(self, user_id: str):
        dfs = _peek("users")
        df = dfs["users"]
        rows = df[df["id"] == user_id]
        if rows.empty:
            return None
        return _row_to_user(rows.iloc[0])

    def create_user(self, full_name: str, email: str, password_hash: str, role: str, program: str = "", student_id: str = "", status: str = "active") -> dict:
        with _db_write():
            dfs = _read_excel()
            normalized = email.lower().strip()
            if normalized in set(dfs["users"]["institutional_email"].astype(str).str.lower().str.strip()):
                # La comprobación de quien llama (auth) pudo quedar obsoleta: dos registros simultáneos
                # con el mismo correo crearían dos cuentas y todo usa la primera que encuentra.
                raise ValueError("Ya existe una cuenta registrada con ese correo.")
            row = {
                "id": _new_id(),
                "full_name": full_name,
                "student_id": student_id,
                "institutional_email": email.lower().strip(),
                "password_hash": password_hash,
                "role": role,
                "program_or_department": program,
                "status": status,
                "created_at": _now_str(),
                "session_epoch": 0,
            }
            dfs["users"] = pd.concat([dfs["users"], pd.DataFrame([row])], ignore_index=True)
            _write_and_sync(dfs)
            return row

    def update_user(self, user_id: str, changes: dict, actor: dict = None):
        """Actualiza solo campos de usuario conocidos y devuelve el registro.

        La vista valida semántica (nombre, correo, programa); esta capa evita
        columnas arbitrarias y garantiza unicidad de correo para cualquier cliente.
        """
        allowed = {
            "full_name", "student_id", "institutional_email", "password_hash",
            "role", "program_or_department", "status",
            "must_change_password", "password_changed_at", "session_epoch",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"Campos de usuario no permitidos: {', '.join(sorted(unknown))}.")
        with _db_write():
            dfs = _read_excel()
            df = dfs["users"]
            idx = df.index[df["id"] == user_id].tolist()
            if not idx:
                return None
            changes = dict(changes)
            old_role = str(df.at[idx[0], "role"] or "")
            if {"role", "status"} & set(changes):
                # Cambiar rol o estado exige un actor con `manage_users`: sin actor
                # no hay a quién atribuir ni qué autorizar (por eso el parámetro
                # es obligatorio para estas dos claves).
                from core import permissions
                if actor is None:
                    raise PermissionError("Cambiar roles o estados requiere un actor autorizado.")
                permissions.require_cap(
                    actor, permissions.CAP_MANAGE_USERS,
                    "Solo el administrador puede cambiar roles o estados.",
                )
                if actor.get("id") == user_id:
                    raise PermissionError("No puedes cambiar tu propio rol ni deshabilitarte.")
            elif actor is not None and actor.get("id") != user_id:
                from core import permissions
                permissions.require_cap(
                    actor, permissions.CAP_MANAGE_USERS,
                    "Solo el administrador puede editar a otra persona.",
                )
            if "institutional_email" in changes:
                email = str(changes["institutional_email"] or "").lower().strip()
                duplicate = df[(df["institutional_email"].str.lower().str.strip() == email) & (df["id"] != user_id)]
                if not duplicate.empty:
                    raise ValueError("Ya existe otra cuenta con ese correo institucional.")
                changes["institutional_email"] = email
            if "role" in changes:
                from core import permissions
                if changes["role"] not in permissions.ROLES:
                    raise ValueError("Rol de usuario inválido.")
            if "status" in changes and changes["status"] not in ("active", "disabled"):
                raise ValueError("Estado de usuario inválido.")
            # Revocación (S4): una clave nueva, o un cambio de rol/estado hecho por
            # el perfil maestro desde «Usuarios» (con `actor`), invalida los tokens
            # abiertos. Sin `actor` se conserva el contrato previo: la sesión ya
            # abierta adopta el rol nuevo en su siguiente recarga.
            revoking = "password_hash" in changes or (
                actor is not None and (
                    ("role" in changes and str(changes["role"]) != old_role)
                    or ("status" in changes and str(changes["status"]) != str(df.at[idx[0], "status"]))
                )
            )
            if revoking and "session_epoch" not in changes:
                changes["session_epoch"] = _int(df.at[idx[0], "session_epoch"]) + 1
            for key, value in changes.items():
                df.at[idx[0], key] = value
            dfs["users"] = df
            if actor is not None and "role" in changes and str(changes["role"]) != old_role:
                audit = _trace_event_row({
                    "event_type": "role_changed", "user_id": user_id, "owner_id": user_id,
                    "actor_id": actor.get("id", ""), "actor_name": actor.get("full_name", ""),
                    "actor_email": actor.get("institutional_email", ""),
                    "actor_role": actor.get("role", ""),
                    "details": json.dumps({"from_role": old_role, "to_role": changes["role"]},
                                          ensure_ascii=False),
                })
                dfs["trace_events"] = pd.concat(
                    [dfs["trace_events"], pd.DataFrame([audit])], ignore_index=True
                )
            _write_and_sync(dfs)
            return _row_to_user(df.loc[idx[0]])

    @firestore_retry
    def get_all_users(self) -> list:
        dfs = _peek("users")
        df = dfs["users"]
        users = [_user_from_dict(record) for record in df.to_dict("records")]
        return sorted(users, key=lambda x: (x.get("full_name") or "").lower())

    def count_users(self) -> int:
        dfs = _peek("users")
        return len(dfs["users"])

    def count_masters(self) -> int:
        dfs = _peek("users")
        df = dfs["users"]
        return len(df[df["role"] == "maestro"])

    # ------------------------------------------------------------------
    # LISTA BLANCA DE PROFESORES
    # ------------------------------------------------------------------

    @firestore_retry
    def is_email_whitelisted_professor(self, email: str) -> bool:
        dfs = _peek("professors_whitelist")
        df = dfs["professors_whitelist"]
        return email.lower().strip() in set(df["institutional_email"].str.lower().str.strip())

    @firestore_retry
    def get_whitelist(self) -> list:
        dfs = _peek("professors_whitelist")
        df = dfs["professors_whitelist"]
        return sorted(df["institutional_email"].tolist())

    def add_to_whitelist(self, email: str, actor: dict = None):
        _authorize(actor, "manage_users", "Solo un administrador modifica la lista blanca.")
        with _db_write():
            dfs = _read_excel()
            df = dfs["professors_whitelist"]
            email = email.lower().strip()
            if email not in set(df["institutional_email"]):
                dfs["professors_whitelist"] = pd.concat(
                    [df, pd.DataFrame([{"institutional_email": email}])], ignore_index=True
                )
                if actor is not None:
                    dfs["trace_events"] = pd.concat([dfs["trace_events"], pd.DataFrame([
                        _admin_audit_row(actor, "whitelist_changed", {"action": "added", "email": email})
                    ])], ignore_index=True)
                _write_and_sync(dfs)

    def remove_from_whitelist(self, email: str, actor: dict = None):
        _authorize(actor, "manage_users", "Solo un administrador modifica la lista blanca.")
        with _db_write():
            dfs = _read_excel()
            df = dfs["professors_whitelist"]
            email = email.lower().strip()
            present = email in set(df["institutional_email"])
            dfs["professors_whitelist"] = df[df["institutional_email"] != email]
            if present and actor is not None:
                dfs["trace_events"] = pd.concat([dfs["trace_events"], pd.DataFrame([
                    _admin_audit_row(actor, "whitelist_changed", {"action": "removed", "email": email})
                ])], ignore_index=True)
            _write_and_sync(dfs)

    # ------------------------------------------------------------------
    # PRESTAMOS (loans)
    # ------------------------------------------------------------------

    def create_loan(self, item: dict, quantity: int, user: dict, expected_return_at=None,
                    notes: str = "", request_id: str = "", operator: dict = None) -> dict:
        if int(quantity) <= 0:
            raise ValueError("La cantidad debe ser mayor a cero.")
        with _db_write():
            dfs = _read_excel()
            current = dfs["items"][dfs["items"]["id"] == item["id"]]
            if not current.empty and current.iloc[0]["status"] == "retired":
                raise ValueError("El producto fue dado de baja: no se puede prestar.")
            # La disponibilidad se re-verifica DENTRO del lock: la comprobacion previa
            # de core.loans.checkout puede haber quedado obsoleta si otra persona
            # saco unidades en el mismo instante (evita prestar mas de lo que hay).
            available = _available_from(dfs, item["id"])
            if available is not None and quantity > available:
                raise ValueError(f"Stock insuficiente. Disponible: {available}.")
            row = {
                "id": _new_id(),
                "item_id": item["id"],
                "item_name": item.get("name", ""),
                "parent_id": item.get("parent_id", "") or "",
                "quantity": quantity,
                "user_id": user["id"],
                "user_name": user.get("full_name", ""),
                "user_role": user.get("role", ""),
                "checkout_at": _now_str(),
                "expected_return_at": expected_return_at.isoformat() if expected_return_at else "",
                "return_at": "",
                "status": "out",
                "notes": notes,
                "request_id": request_id,
                "operator_id": (operator or {}).get("id", ""),
                "operator_name": (operator or {}).get("full_name", ""),
            }
            dfs["loans"] = pd.concat([dfs["loans"], pd.DataFrame([row])], ignore_index=True)

            dispatched = f" · despachado por {operator.get('full_name')}" if operator and operator.get("full_name") else ""
            hist_row = {
                "id": _new_id(), "item_id": item["id"], "timestamp": _now_str(),
                "type": "Salida", "quantity_change": -quantity,
                "actor_user_id": user.get("institutional_email", ""),
                "details": f"Prestado a {user.get('full_name', '')} ({row['id']}){dispatched}",
            }
            dfs["item_history"] = pd.concat([dfs["item_history"], pd.DataFrame([hist_row])], ignore_index=True)
            _write_and_sync(dfs)
            return row

    def return_loan(self, loan_id: str, actor_email: str = "", actor: dict = None,
                    quantity: int = None, note: str = ""):
        """Reingresa un préstamo. Con ``actor`` solo quien tiene `checkin_any_loan` reingresa directo
        (el cliente devuelve por el flujo de `core/returns.py`, que llama con ``actor=None`` tras su
        propia verificación). ``quantity`` menor a la prestada es una devolución parcial: el préstamo
        sigue abierto por lo que falta."""
        with _db_write():
            dfs = _read_excel()
            df = dfs["loans"]
            idx = df.index[df["id"] == loan_id].tolist()
            if not idx:
                return False, "El prestamo no existe."
            i = idx[0]
            if actor is not None:
                from core import permissions
                if not permissions.has_cap(actor, permissions.CAP_CHECKIN_ANY):
                    raise PermissionError("No puedes reingresar un préstamo directamente.")
            if str(df.at[i, "return_at"]).strip() not in ("", "nan", "None"):
                return False, "Este prestamo ya fue devuelto."

            item_id = df.at[i, "item_id"]
            borrowed = int(float(df.at[i, "quantity"] or 0))
            qty = borrowed if quantity is None else int(quantity)
            if qty <= 0 or qty > borrowed:
                return False, f"La cantidad devuelta debe estar entre 1 y {borrowed}."
            partial = qty < borrowed
            if partial:
                df.at[i, "quantity"] = borrowed - qty
            else:
                df.at[i, "return_at"] = _now_str()
                df.at[i, "status"] = "returned"
            dfs["loans"] = df

            details = f"Reingreso de prestamo {loan_id}" + (f" ({qty} de {borrowed})" if partial else "")
            if note:
                details += f" · {note}"
            hist_row = {
                "id": _new_id(), "item_id": item_id, "timestamp": _now_str(),
                "type": "Reingreso", "quantity_change": qty,
                "actor_user_id": actor_email, "details": details,
            }
            dfs["item_history"] = pd.concat([dfs["item_history"], pd.DataFrame([hist_row])], ignore_index=True)
            _write_and_sync(dfs)
            if partial:
                return True, f"Reingreso parcial: {qty} de {borrowed}. Quedan {borrowed - qty} por devolver."
            return True, "Reingreso registrado correctamente."

    @firestore_retry
    def get_open_loans_for_item(self, item_id: str) -> list:
        dfs = _peek("loans")
        df = dfs["loans"]
        rows = df[(df["item_id"] == item_id) & (df["status"] == "out")]
        return [_loan_from_dict(record) for record in rows.to_dict("records")]

    @firestore_retry
    def get_open_loans_for_user(self, user_id: str) -> list:
        dfs = _peek("loans")
        df = dfs["loans"]
        rows = df[(df["user_id"] == user_id) & (df["status"] == "out")]
        loans = [_loan_from_dict(record) for record in rows.to_dict("records")]
        return sorted(loans, key=lambda x: x.get("checkout_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    @firestore_retry
    def get_all_loans(self, status: str = None) -> list:
        dfs = _peek("loans")
        df = dfs["loans"]
        if df.empty:
            return []
        if status:
            df = df[df["status"] == status]
        loans = [_loan_from_dict(record) for record in df.to_dict("records")]
        return sorted(loans, key=lambda x: x.get("checkout_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    @firestore_retry
    def get_item_history(self, item_id: str) -> list:
        dfs = _peek("item_history")
        df = dfs["item_history"]
        rows = df[df["item_id"] == item_id]
        history = [_clean_nan(record) for record in rows.to_dict("records")]
        return sorted(history, key=lambda x: x.get("timestamp") or "", reverse=True)
    # ------------------------------------------------------------------
    # RESERVAS DE LABORATORIO
    # ------------------------------------------------------------------

    @staticmethod
    def _approved_reservations(dfs: dict) -> list:
        df = dfs["reservations"]
        return [_reservation_from_dict(record) for record in df[df["status"] == "approved"].to_dict("records")]

    def create_reservation(self, data: dict, user: dict) -> dict:
        with _db_write():
            dfs = _read_excel()
            # Conflicto re-verificado dentro del lock (la comprobacion previa del
            # dominio pudo quedar obsoleta por otra aprobacion simultanea).
            conflict = reservation_rules.find_conflict(self._approved_reservations(dfs), data)
            if conflict:
                raise ValueError(
                    f"Existe una reserva aprobada que se cruza con ese horario: {conflict.get('activity')}."
                )
            now = _now_str()
            row = {
                "id": _new_id(), "scope_type": data["scope_type"],
                "activity": data.get("activity", ""), "purpose": data.get("purpose", ""),
                "attendees": int(data.get("attendees", 1)),
                "start_at": data["start_at"].isoformat(), "end_at": data["end_at"].isoformat(),
                "requester_id": user["id"], "requester_name": user.get("full_name", ""),
                "requester_email": user.get("institutional_email", ""), "status": "pending",
                "reviewed_by": "", "reviewed_at": "", "review_notes": "",
                "email_notified": False, "email_error": "", "created_at": now, "updated_at": now,
            }
            dfs["reservations"] = pd.concat([dfs["reservations"], pd.DataFrame([row])], ignore_index=True)
            _write_and_sync(dfs)
            return row

    @firestore_retry
    def get_reservation(self, reservation_id: str):
        dfs = _peek("reservations")
        rows = dfs["reservations"][dfs["reservations"]["id"] == reservation_id]
        return None if rows.empty else _row_to_reservation(rows.iloc[0])

    @firestore_retry
    def get_reservations(self, status: str = None, user_id: str = None) -> list:
        df = _peek("reservations")["reservations"]
        if status:
            df = df[df["status"] == status]
        if user_id:
            df = df[df["requester_id"] == user_id]
        rows = [_reservation_from_dict(record) for record in df.to_dict("records")]
        aware_min = datetime.min.replace(tzinfo=timezone.utc)
        return sorted(rows, key=lambda row: row.get("start_at") or aware_min)

    def update_reservation_status(self, reservation_id: str, status: str,
                                  reviewer_email: str, notes: str = "") -> bool:
        with _db_write():
            dfs = _read_excel()
            df = dfs["reservations"]
            idx = df.index[df["id"] == reservation_id].tolist()
            if not idx:
                return False
            if status in ("approved", "rejected") and df.at[idx[0], "status"] != "pending":
                # Dos personas revisando a la vez: gana la primera, la otra no la pisa.
                raise ValueError("La reserva ya fue revisada por otra persona.")
            if status == "approved":
                conflict = reservation_rules.find_conflict(
                    self._approved_reservations(dfs), _row_to_reservation(df.loc[idx[0]]),
                    exclude_id=reservation_id,
                )
                if conflict:
                    raise ValueError(f"Conflicto con la reserva aprobada: {conflict.get('activity')}.")
            i, now = idx[0], _now_str()
            df.at[i, "status"] = status
            df.at[i, "reviewed_by"] = reviewer_email
            df.at[i, "reviewed_at"] = now
            df.at[i, "review_notes"] = notes
            df.at[i, "updated_at"] = now
            dfs["reservations"] = df
            _write_and_sync(dfs)
            return True

    def update_reservation_notification(self, reservation_id: str, sent: bool, error: str = "") -> bool:
        with _db_write():
            dfs = _read_excel()
            df = dfs["reservations"]
            idx = df.index[df["id"] == reservation_id].tolist()
            if not idx:
                return False
            df.at[idx[0], "email_notified"] = bool(sent)
            df.at[idx[0], "email_error"] = error
            df.at[idx[0], "updated_at"] = _now_str()
            dfs["reservations"] = df
            _write_and_sync(dfs)
            return True

    # ------------------------------------------------------------------
    # SOLICITUDES DE PRODUCTOS Y SERVICIOS
    # ------------------------------------------------------------------

    @staticmethod
    def _service_request_row(data: dict, user: dict, now: str, order_id: str = "") -> dict:
        return {
            "id": _new_id(), "request_type": data["request_type"],
            "item_id": data.get("item_id", ""), "item_name": data.get("item_name", ""),
            "quantity": int(data.get("quantity", 0)), "service_name": data.get("service_name", ""),
            "description": data.get("description", ""), "needed_at": data.get("needed_at", ""),
            "requester_id": user["id"], "requester_name": user.get("full_name", ""),
            "requester_email": user.get("institutional_email", ""), "status": "pending",
            "reviewed_by": "", "reviewed_at": "", "review_notes": "",
            "email_notified": False, "email_error": "", "created_at": now, "updated_at": now,
            "order_id": order_id or data.get("order_id", ""),
        }

    def create_service_request(self, data: dict, user: dict) -> dict:
        return self.create_service_requests([data], user)[0]

    def create_service_requests(self, rows: list, user: dict, order_id: str = "") -> list:
        """Crea varias solicitudes en UNA escritura (un pedido de varios productos: todo o nada)."""
        with _db_write():
            dfs = _read_excel()
            now = _now_str()
            created = [self._service_request_row(data, user, now, order_id) for data in rows]
            dfs["service_requests"] = pd.concat(
                [dfs["service_requests"], pd.DataFrame(created)], ignore_index=True
            )
            _write_and_sync(dfs)
            return created

    @firestore_retry
    def get_service_request(self, request_id: str):
        dfs = _peek("service_requests")
        rows = dfs["service_requests"][dfs["service_requests"]["id"] == request_id]
        return None if rows.empty else _row_to_service_request(rows.iloc[0])

    @firestore_retry
    def get_service_requests(self, status: str = None, user_id: str = None, exclude_ids=None) -> list:
        df = _peek("service_requests")["service_requests"]
        if status:
            df = df[df["status"] == status]
        if user_id:
            df = df[df["requester_id"] == user_id]
        if exclude_ids:
            df = df[~df["id"].isin(set(exclude_ids))]   # vectorizado: no se arman dicts de lo descartado
        rows = [_service_request_from_dict(record) for record in df.to_dict("records")]
        aware_min = datetime.min.replace(tzinfo=timezone.utc)
        return sorted(rows, key=lambda row: row.get("created_at") or aware_min, reverse=True)

    def update_service_request_status(self, request_id: str, status: str,
                                      reviewer_email: str, notes: str = "") -> bool:
        with _db_write():
            dfs = _read_excel()
            df = dfs["service_requests"]
            idx = df.index[df["id"] == request_id].tolist()
            if not idx:
                return False
            i, now = idx[0], _now_str()
            df.at[i, "status"] = status
            df.at[i, "reviewed_by"] = reviewer_email
            df.at[i, "reviewed_at"] = now
            df.at[i, "review_notes"] = notes
            df.at[i, "updated_at"] = now
            dfs["service_requests"] = df
            _write_and_sync(dfs)
            return True

    def update_service_request_notification(self, request_id: str, sent: bool, error: str = "") -> bool:
        with _db_write():
            dfs = _read_excel()
            df = dfs["service_requests"]
            idx = df.index[df["id"] == request_id].tolist()
            if not idx:
                return False
            df.at[idx[0], "email_notified"] = bool(sent)
            df.at[idx[0], "email_error"] = error
            df.at[idx[0], "updated_at"] = _now_str()
            dfs["service_requests"] = df
            _write_and_sync(dfs)
            return True

    def transition_request(self, request_id: str, event_type: str, actor: dict,
                           details: dict = None):
        """Valida y aplica una transición de solicitud en UNA escritura.

        Devuelve ``(ok, mensaje, evento)``. La validación (permisos, etapa,
        autorización, lecturas y stock) ocurre dentro del bloqueo con datos
        frescos (`core.dispatch.plan_transition`). Un intento rechazado solo
        agrega un evento de auditoría: nunca cambia etapa, solicitud, préstamo
        ni inventario.
        """
        from core import dispatch

        actor = dict(actor or {})
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        with _db_write():
            dfs = _read_excel()
            requests = dfs["service_requests"]
            indexes = requests.index[requests["id"] == request_id].tolist()
            if not indexes:
                return False, "La solicitud no existe.", None
            i = indexes[0]
            request = _row_to_service_request(requests.loc[i])
            event_df = dfs["trace_events"]
            mask = event_df["request_id"] == request_id
            request_events = sorted(
                (_clean_nan(row.to_dict()) for _, row in event_df[mask].iterrows()),
                key=lambda row: (str(row.get("created_at") or ""), _int(row.get("seq"))),
            )
            seq_values = pd.to_numeric(event_df.loc[mask, "seq"], errors="coerce").dropna()
            next_seq = int(seq_values.max()) + 1 if not seq_values.empty else 1

            item_id = request.get("item_id", "") or ""
            loans = dfs["loans"]
            open_rows = loans[(loans["request_id"] == request_id) & (loans["status"] == "out")]
            open_loan = _row_to_loan(open_rows.iloc[0]) if not open_rows.empty else None
            available = _available_from(dfs, item_id) if item_id else None
            plan = dispatch.plan_transition(
                request, request_events, actor, event_type, details,
                available=available, open_loan=open_loan,
            )

            def event_row(kind, payload, loan_id=""):
                nonlocal next_seq
                row = _trace_event_row({
                    "event_type": kind, "request_id": request_id, "seq": next_seq,
                    "item_id": item_id, "loan_id": loan_id,
                    "user_id": request.get("requester_id", ""),
                    "owner_id": request.get("requester_id", ""),
                    "actor_id": actor.get("id", ""), "actor_name": actor.get("full_name", ""),
                    "actor_email": actor.get("institutional_email", ""),
                    "actor_role": actor.get("role", ""), "receipt": "",
                    "details": json.dumps(payload, ensure_ascii=False, default=str),
                    "created_at": now,
                })
                next_seq += 1
                return row

            if not plan["ok"]:
                row = event_row(plan["failure_kind"], plan["failure_details"])
                dfs["trace_events"] = pd.concat(
                    [dfs["trace_events"], pd.DataFrame([row])], ignore_index=True
                )
                _write_and_sync(dfs)
                return False, plan["message"], dict(row)

            quantity = _int(request.get("quantity"))
            loan_id = plan.get("loan_id", "")
            if plan["loan_action"] == "create":
                loan_id = _new_id()
                users = dfs["users"]
                requester = users[users["id"] == request.get("requester_id", "")]
                item_rows = dfs["items"][dfs["items"]["id"] == item_id]
                loan = {
                    "id": loan_id, "item_id": item_id, "item_name": request.get("item_name", ""),
                    "parent_id": (item_rows.iloc[0].get("parent_id", "") or "") if not item_rows.empty else "",
                    "quantity": quantity, "user_id": request.get("requester_id", ""),
                    "user_name": request.get("requester_name", ""),
                    "user_role": (requester.iloc[0].get("role", "") or "") if not requester.empty else "",
                    "checkout_at": now, "expected_return_at": "", "return_at": "",
                    "status": "out", "notes": f"Picking de la solicitud {request_id}",
                    "request_id": request_id,
                    "operator_id": actor.get("id", ""), "operator_name": actor.get("full_name", ""),
                }
                dfs["loans"] = pd.concat([dfs["loans"], pd.DataFrame([loan])], ignore_index=True)
                history = {
                    "id": _new_id(), "item_id": item_id, "timestamp": now,
                    "type": "Salida", "quantity_change": -quantity,
                    "actor_user_id": actor.get("institutional_email", ""),
                    "details": f"Picking de la solicitud {request_id} a nombre de "
                               f"{request.get('requester_name', '')} ({loan_id}) · "
                               f"despachado por {actor.get('full_name', '') or 'un operario'}",
                }
                dfs["item_history"] = pd.concat(
                    [dfs["item_history"], pd.DataFrame([history])], ignore_index=True
                )
            elif plan["loan_action"] == "reuse":
                # Re-picking tras «volver al picking»: las unidades ya salieron; el
                # préstamo conserva su salida y pasa a nombre del operario que lo rehízo.
                loan_index = dfs["loans"].index[dfs["loans"]["id"] == loan_id].tolist()[0]
                dfs["loans"].at[loan_index, "operator_id"] = actor.get("id", "")
                dfs["loans"].at[loan_index, "operator_name"] = actor.get("full_name", "")
            elif plan["loan_action"] == "close":
                loan_index = dfs["loans"].index[dfs["loans"]["id"] == loan_id].tolist()[0]
                returned = _int(dfs["loans"].at[loan_index, "quantity"])
                dfs["loans"].at[loan_index, "return_at"] = now
                dfs["loans"].at[loan_index, "status"] = "returned"
                history = {
                    "id": _new_id(), "item_id": item_id, "timestamp": now,
                    "type": "Reingreso", "quantity_change": returned,
                    "actor_user_id": actor.get("institutional_email", ""),
                    "details": f"Devuelto al estante desde la solicitud {request_id} ({loan_id})",
                }
                dfs["item_history"] = pd.concat(
                    [dfs["item_history"], pd.DataFrame([history])], ignore_index=True
                )

            if plan["status"]:
                requests.at[i, "status"] = plan["status"]
                requests.at[i, "reviewed_by"] = actor.get("institutional_email", "")
                requests.at[i, "reviewed_at"] = now
                requests.at[i, "review_notes"] = plan["details"].get("notes", "") or plan["details"].get("reason", "")
                requests.at[i, "updated_at"] = now
                dfs["service_requests"] = requests

            row = event_row(event_type, plan["details"], loan_id)
            rows = [row] + [event_row(kind, payload, loan_id) for kind, payload in plan["extra_events"]]
            dfs["trace_events"] = pd.concat(
                [dfs["trace_events"], pd.DataFrame(rows)], ignore_index=True
            )
            _write_and_sync(dfs)
            return True, plan["message"], dict(row)

    REVOKED_SESSIONS_KEY = "revoked_sessions"
    REVOKED_SESSIONS_LIMIT = 1000

    # Datos operativos que el administrador puede vaciar desde la app (Usuarios → Datos).
    PURGE_NOISE_EVENTS = ("session_login", "session_logout", "session_expired", "nfc_tap")

    def operational_data_counts(self) -> dict:
        """Cuántas filas hay de cada tipo de dato operativo (vista previa del borrado)."""
        dfs = _peek("service_requests", "reservations", "notification_log", "trace_events", "loans")
        events = dfs["trace_events"]
        loans = dfs["loans"]
        return {
            "solicitudes": len(dfs["service_requests"]),
            "trazabilidad": int((events["request_id"] != "").sum()),
            "reservas": len(dfs["reservations"]),
            "correos": len(dfs["notification_log"]),
            "sesiones_nfc": int(events["event_type"].isin(self.PURGE_NOISE_EVENTS).sum()),
            "prestamos_cerrados": int((loans["status"] != "out").sum()),
            "prestamos_abiertos": int((loans["status"] == "out").sum()),
        }

    def purge_operational_data(self, actor: dict, requests: bool = True, reservations: bool = True,
                               mail_log: bool = True, session_noise: bool = True,
                               closed_loans: bool = False) -> dict:
        """Vacía datos operativos de una base **real**, solo a petición del administrador.

        Nunca toca personas, productos, ubicaciones, fotos, ajustes, ni los préstamos abiertos (de ellos
        depende la disponibilidad). Guarda antes ``<base>.bak`` y deja constancia en la trazabilidad.
        Devuelve cuántas filas quitó de cada tipo."""
        _authorize(actor, "purge_data", "Solo un administrador puede borrar datos operativos.")
        removed = {}
        with _db_write():
            dfs = _read_excel()
            _backup_local_copy()
            if requests:
                removed["solicitudes"] = len(dfs["service_requests"])
                dfs["service_requests"] = dfs["service_requests"].iloc[0:0].copy()
                events = dfs["trace_events"]
                linked = events["request_id"] != ""
                removed["trazabilidad"] = int(linked.sum())
                dfs["trace_events"] = events[~linked].reset_index(drop=True)
                loans = dfs["loans"].copy()          # un préstamo abierto ya no apunta a una solicitud borrada
                loans["request_id"] = ""
                dfs["loans"] = loans
            if reservations:
                removed["reservas"] = len(dfs["reservations"])
                dfs["reservations"] = dfs["reservations"].iloc[0:0].copy()
            if mail_log:
                removed["correos"] = len(dfs["notification_log"])
                dfs["notification_log"] = dfs["notification_log"].iloc[0:0].copy()
            if session_noise:
                events = dfs["trace_events"]
                noise = events["event_type"].isin(self.PURGE_NOISE_EVENTS)
                removed["sesiones_nfc"] = int(noise.sum())
                dfs["trace_events"] = events[~noise].reset_index(drop=True)
            if closed_loans:
                loans = dfs["loans"]
                done = loans["status"] != "out"
                removed["prestamos_cerrados"] = int(done.sum())
                dfs["loans"] = loans[~done].reset_index(drop=True)
            if actor is not None:
                audit = _admin_audit_row(actor, "data_purged", {"removed": removed})
                dfs["trace_events"] = pd.concat([dfs["trace_events"], pd.DataFrame([audit])], ignore_index=True)
            _write_and_sync(dfs)
            _bump_data_version(reload=True)       # las listas de Despacho se recalculan desde cero
        from core import dispatch, picking

        dispatch.reset_orders_cache()
        picking.reset()
        return removed

    def reset_simulation(self) -> dict:
        """Vacía lo operativo (préstamos, solicitudes, eventos, reservas, correos y las
        salidas/reingresos del historial) de una base de SIMULACIÓN de la lúdica.

        Se niega con ``PermissionError`` si la base no lleva la marca de simulación
        (la que escribe ``core.ludica.seed_simulation``): nunca vacía una base real.
        Conserva personas, productos y ajustes. Devuelve cuántas filas quitó por hoja."""
        if str(self.get_setting("ludica_sandbox", "") or "").strip() != "1":
            raise PermissionError("Esta base no está marcada como simulación de la lúdica: no se reinicia.")
        removed = {}
        with _db_write():
            dfs = _read_excel()
            for sheet in ("loans", "service_requests", "trace_events", "reservations", "notification_log"):
                removed[sheet] = len(dfs[sheet])
                dfs[sheet] = dfs[sheet].iloc[0:0].copy()
            history = dfs["item_history"]
            kept = history[history["type"] == "Alta"].reset_index(drop=True)
            removed["item_history"] = len(history) - len(kept)
            dfs["item_history"] = kept
            settings = dfs["settings"]
            dfs["settings"] = settings[settings["key"] != self.REVOKED_SESSIONS_KEY].reset_index(drop=True)
            _write_and_sync(dfs)
        return removed

    def get_revoked_sessions(self, now: int = None) -> dict:
        """``{sid: vencimiento}`` de las sesiones cerradas que aún no vencen."""
        moment = int(now if now is not None else time.time())
        try:
            data = json.loads(self.get_setting(self.REVOKED_SESSIONS_KEY, "") or "{}")
        except (TypeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(sid): int(exp) for sid, exp in data.items()
                if isinstance(exp, (int, float)) and int(exp) > moment}

    def add_revoked_session(self, sid: str, exp: int, now: int = None) -> None:
        """Recuerda un cierre de sesión hasta que el token habría vencido, aunque la app
        se reinicie. Todo el ciclo leer-modificar-escribir va bajo el bloqueo de datos."""
        sid = str(sid or "").strip()
        if not sid:
            return
        with _db_write():
            current = self.get_revoked_sessions(now)
            current[sid] = int(exp)
            if len(current) > self.REVOKED_SESSIONS_LIMIT:      # conserva las que vencen más tarde
                keep = sorted(current, key=current.get, reverse=True)[:self.REVOKED_SESSIONS_LIMIT]
                current = {key: current[key] for key in keep}
            dfs = _read_excel()
            df = dfs["settings"]
            row = {"key": self.REVOKED_SESSIONS_KEY, "value": json.dumps(current, separators=(",", ":")),
                   "updated_by": "session", "updated_at": _now_str()}
            idx = df.index[df["key"] == self.REVOKED_SESSIONS_KEY].tolist()
            if idx:
                for column, cell in row.items():
                    df.at[idx[0], column] = cell
            else:
                df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
            dfs["settings"] = df
            _write_and_sync(dfs)

    def revoke_user_sessions(self, user_id: str, actor: dict) -> dict:
        """Incrementa el epoch propio o de otro usuario si el actor administra usuarios."""
        from core import permissions
        target = self.get_user_by_id(user_id)
        if not target:
            raise ValueError("La cuenta no existe.")
        if actor.get("id") != user_id:
            permissions.require_cap(actor, permissions.CAP_MANAGE_USERS)
        try:
            epoch = int(float(target.get("session_epoch") or 0)) + 1
        except (TypeError, ValueError):
            epoch = 1
        return self.update_user(user_id, {"session_epoch": epoch})

    # ------------------------------------------------------------------
    # CONFIGURACION DEL LABORATORIO (clave / valor)
    # ------------------------------------------------------------------

    @firestore_retry
    def get_setting(self, key: str, default=None):
        df = _peek("settings")["settings"]
        rows = df[df["key"] == key]
        if rows.empty:
            return default
        value = rows.iloc[0]["value"]
        return default if value in (None, "") else value

    def set_setting(self, key: str, value: str, actor_email: str = "") -> None:
        """Crea o reemplaza un valor de configuracion y lo publica en el momento."""
        with _db_write():
            dfs = _read_excel()
            df = dfs["settings"]
            row = {"key": key, "value": value, "updated_by": actor_email, "updated_at": _now_str()}
            idx = df.index[df["key"] == key].tolist()
            if idx:
                for column, cell in row.items():
                    df.at[idx[0], column] = cell
            else:
                df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
            dfs["settings"] = df
            _write_and_sync(dfs)

    # ------------------------------------------------------------------
    # TRAZABILIDAD (eventos; ver core/traceability.py)
    # ------------------------------------------------------------------

    def add_trace_event(self, event: dict) -> dict:
        """Append-only. Asigna ``seq`` por solicitud dentro del bloqueo."""
        with _db_write():
            dfs = _read_excel()
            payload = dict(event or {})
            payload.setdefault("owner_id", payload.get("user_id", ""))
            payload.setdefault("user_id", payload.get("owner_id", ""))
            request_id = str(payload.get("request_id") or "")
            if request_id and not str(payload.get("seq") or "").strip():
                current = dfs["trace_events"]
                values = pd.to_numeric(
                    current.loc[current["request_id"] == request_id, "seq"], errors="coerce"
                ).dropna()
                payload["seq"] = int(values.max()) + 1 if not values.empty else 1
            row = _trace_event_row(payload)
            dfs["trace_events"] = pd.concat(
                [dfs["trace_events"], pd.DataFrame([row])], ignore_index=True
            )
            _write_and_sync(dfs)
        return dict(row)

    @firestore_retry
    def get_trace_events(self, request_id: str = None, item_id: str = None, user_id: str = None,
                         event_type: str = None, loan_id: str = None, receipt: str = None,
                         owner_id: str = None) -> list:
        """Eventos que cumplen TODOS los filtros dados, del más antiguo al más reciente."""
        source = _load_cache()["trace_events"]
        if request_id is not None:
            positions = _trace_positions(source, request_id)
            df = source.loc[positions] if positions else source.iloc[0:0]
        else:
            df = source
        filters = {"item_id": item_id, "user_id": user_id,      # request_id ya se resolvió con el índice
                   "owner_id": owner_id, "event_type": event_type,
                   "loan_id": loan_id, "receipt": receipt}
        for column, value in filters.items():
            if value is not None:
                df = df[df[column] == value]
        rows = [_clean_nan(record) for record in df.to_dict("records")]
        return sorted(rows, key=lambda row: (str(row.get("created_at") or ""), _int(row.get("seq"))))

    def get_health(self) -> dict:
        """Cómo está la base: tamaño, filas por hoja, velocidad de escritura y estado de la sincronización."""
        try:
            size = os.path.getsize(EXCEL_PATH) / 1_000_000
        except OSError:
            size = 0.0
        frames = _peek()
        rows = {sheet: len(frame) for sheet, frame in frames.items()}
        return {"workbook_mb": round(size, 2), "rows": rows, "cells": sum(len(frames[s]) * len(frames[s].columns) for s in frames),
                "write": write_stats(), "sync": get_sync_status()}

    def get_events_index(self) -> dict:
        """``{request_id: [eventos]}`` (del más antiguo al más reciente) de TODAS las solicitudes.

        Los listados que recorren muchos pedidos (tiempos de despacho, atascados, conciliación) pedían
        los eventos de cada uno por separado: con miles de pedidos tardaban segundos. Aquí se arma una
        sola vez por versión de los datos. **Solo lectura**: las listas se comparten entre sesiones."""
        version = data_state()["version"]
        with _events_index_lock:
            if _events_index["version"] == version:
                return _events_index["index"]
        index = {}
        for event in self.get_trace_events():
            index.setdefault(event.get("request_id") or "", []).append(event)
        with _events_index_lock:
            _events_index.update(version=version, index=index)
        return index

    # ------------------------------------------------------------------
    # REGISTRO DE CORREOS AUTOMATICOS (ver core/email_events.py)
    # ------------------------------------------------------------------

    def add_notification_log(self, entry: dict) -> dict:
        """Agrega UN registro de correo (una escritura = un commit) y conserva solo
        los ultimos NOTIFICATION_LOG_LIMIT. Solo se guardan las columnas conocidas."""
        columns = SHEET_COLUMNS["notification_log"]
        row = {column: "" for column in columns}
        row.update({key: "" if value is None else str(value) for key, value in entry.items() if key in columns})
        row["id"] = _new_id()
        row["created_at"] = row["created_at"] or _now_str()
        with _db_write():
            dfs = _read_excel()
            df = pd.concat([dfs["notification_log"], pd.DataFrame([row])], ignore_index=True)
            if len(df) > NOTIFICATION_LOG_LIMIT:
                df = df.iloc[-NOTIFICATION_LOG_LIMIT:].reset_index(drop=True)
            dfs["notification_log"] = df
            _write_and_sync(dfs)
        return dict(row)

    @firestore_retry
    def get_notification_log(self, limit: int = 50) -> list:
        """Registros de correo del mas reciente al mas antiguo (todos si limit=0)."""
        df = _peek("notification_log")["notification_log"]
        rows = [_clean_nan(record) for record in df.to_dict("records")]
        rows.sort(key=lambda row: row.get("created_at") or "", reverse=True)
        return rows[:limit] if limit else rows

    # ------------------------------------------------------------------
    # ALTA DE VARIOS ITEMS NUEVOS EN UNA SOLA ESCRITURA
    # ------------------------------------------------------------------

    @firestore_retry
    def save_items_bulk(self, items: list, actor_email: str = "", details: str = None,
                        actor: dict = None) -> list:
        """Crea varios items NUEVOS con UNA sola lectura + escritura + sincronizacion
        (un solo commit en GitHub en vez de uno por producto).

        Todo o nada: cada fila se valida con las reglas de `save_item` (formato del
        codigo, tipo, Contenedor Principal existente, codigo libre) mas nombre
        obligatorio, contenedor activo, codigo no repetido en la tanda y cantidades
        enteras no negativas. Si alguna falla se lanza ValueError con TODOS los
        errores y no se escribe nada. Como en `save_item`, un codigo dado de baja se
        reutiliza (se borra su rastro anterior). Un item puede apuntar a un
        Contenedor Principal creado antes en la misma tanda. Cada alta queda en el
        historial ("Alta", con el `details` de la fila o el general). Devuelve los
        codigos creados, en orden."""
        items = list(items or [])
        only_locations = bool(items) and all(row.get("item_type") == LOCATION_TYPE for row in items)
        _authorize(actor, "manage_locations" if only_locations else "edit_inventory",
                   "No tienes permiso para crear ubicaciones." if only_locations
                   else "No tienes permiso para crear ítems.")
        if not items:
            return []

        def whole(value, field):
            raw = "" if value is None else str(value).strip()
            if raw.lower() in ("", "nan", "none"):
                return 0
            try:
                number = float(raw)
            except ValueError:
                raise ValueError(f"{field} debe ser un numero entero.") from None
            if not number.is_integer() or number < 0:
                raise ValueError(f"{field} debe ser un numero entero mayor o igual a 0.")
            return int(number)

        with _db_write():
            dfs = _read_excel()
            df_items = dfs["items"]
            rows, errors, reused, batch_types = [], [], [], {}
            for position, raw in enumerate(items, start=1):
                custom_id = str(raw.get("id", "") or "").strip()
                name = str(raw.get("name", "") or "").strip()
                label = f"Fila {position} ({custom_id or 'sin codigo'})"
                if not custom_id or not name:
                    errors.append(f"{label}: codigo y nombre son obligatorios.")
                    continue
                if custom_id in batch_types:
                    errors.append(f"{label}: el codigo esta repetido en la misma tanda.")
                    continue
                existing = df_items[df_items["id"] == custom_id]
                if not existing.empty and existing.iloc[0]["status"] != "retired":
                    errors.append(f"{label}: Ya existe un item con ese codigo.")
                    continue

                item_type = str(raw.get("item_type", "standalone") or "standalone").strip()
                parent_id = str(raw.get("parent_id", "") or "").strip()
                try:
                    quantity = whole(raw.get("quantity", 0), "Cantidad")
                    min_alert = whole(raw.get("min_stock_alert", 0), "Umbral de alerta")
                    if item_type == LOCATION_TYPE:
                        quantity = min_alert = 0  # una ubicacion no tiene stock
                    lookup = df_items
                    if parent_id in batch_types:
                        # Contenedor creado antes en esta misma tanda (va primero en la busqueda).
                        lookup = pd.concat([pd.DataFrame(rows), df_items], ignore_index=True)
                    _validate_item_data({"item_type": item_type, "parent_id": parent_id},
                                        lookup, custom_id, is_new=True)
                    if parent_id and parent_id not in batch_types:
                        parent_rows = df_items[df_items["id"] == parent_id]
                        if parent_rows.iloc[0]["status"] == "retired":
                            raise ValueError(f"El Contenedor Principal '{parent_id}' esta dado de baja.")
                except ValueError as exc:
                    errors.append(f"{label}: {exc}")
                    continue

                batch_types[custom_id] = item_type
                if not existing.empty:
                    reused.append(custom_id)
                rows.append({
                    "id": custom_id,
                    "name": name,
                    "category": str(raw.get("category", "") or ""),
                    "description": str(raw.get("description", "") or ""),
                    "item_type": item_type,
                    "parent_id": parent_id,
                    "unit": str(raw.get("unit", "") or "").strip() or "unidad",
                    "quantity": quantity,
                    "location": str(raw.get("location", "") or ""),
                    "min_stock_alert": min_alert,
                    "status": "active",
                    "created_by": raw.get("created_by") or actor_email,
                    "updated_at": _now_str(),
                })

            if errors:
                raise ValueError("No se creo ningun item. " + " ".join(errors))

            for custom_id in reused:
                # Codigo liberado por una baja anterior: se reutiliza como item nuevo.
                _purge_item_records(dfs, _cascade_ids(dfs["items"], custom_id), actor_email)

            now = _now_str()  # sin errores, `rows` corresponde 1 a 1 con `items`
            history = [{
                "id": _new_id(), "item_id": row["id"], "timestamp": now, "type": "Alta",
                "quantity_change": row["quantity"], "actor_user_id": actor_email,
                "details": raw.get("details") or details or "Item creado en el sistema.",
            } for row, raw in zip(rows, items)]
            dfs["items"] = pd.concat([dfs["items"], pd.DataFrame(rows)], ignore_index=True)
            dfs["item_history"] = pd.concat([dfs["item_history"], pd.DataFrame(history)], ignore_index=True)
            _write_and_sync(dfs)
            logger.info(f"{len(rows)} item(s) creados en una sola escritura por {actor_email or 'sistema'}.")
        return [row["id"] for row in rows]

    # ------------------------------------------------------------------
    # FOTOS DE LOS OBJETOS (indice; los JPEG viven en el repo, ver core/photos.py)
    # ------------------------------------------------------------------

    def add_item_photo(self, photo: dict, trace_event: dict = None) -> dict:
        """Agrega UNA foto al indice `item_photos` y, si se da, su evento de
        trazabilidad, en la MISMA escritura (un solo commit). Solo se guardan las
        columnas conocidas. ValueError si el item no existe."""
        columns = SHEET_COLUMNS["item_photos"]
        row = {column: "" for column in columns}
        row.update({key: "" if value is None else str(value) for key, value in photo.items() if key in columns})
        row["id"] = row["id"] or _new_id()
        row["taken_at"] = row["taken_at"] or _now_str()
        if not row["item_id"] or not row["path"]:
            raise ValueError("La foto necesita el codigo del item y la ruta del archivo.")
        event_row = _trace_event_row(trace_event) if trace_event else None
        with _db_write():
            dfs = _read_excel()
            if dfs["items"][dfs["items"]["id"] == row["item_id"]].empty:
                raise ValueError("El item no existe.")
            dfs["item_photos"] = pd.concat([dfs["item_photos"], pd.DataFrame([row])], ignore_index=True)
            if event_row:
                dfs["trace_events"] = pd.concat([dfs["trace_events"], pd.DataFrame([event_row])], ignore_index=True)
            _write_and_sync(dfs)
        return dict(row)

    @firestore_retry
    def get_item_photos(self, item_id: str) -> list:
        """Fotos de un item, la mas reciente primero."""
        df = _peek("item_photos")["item_photos"]
        rows = [_clean_nan(record) for record in df[df["item_id"] == item_id].to_dict("records")]
        return sorted(rows, key=lambda row: row.get("taken_at") or "", reverse=True)

    @firestore_retry
    def get_item_photo(self, photo_id: str):
        df = _peek("item_photos")["item_photos"]
        rows = df[df["id"] == photo_id]
        return None if rows.empty else _clean_nan(rows.iloc[0].to_dict())

    def get_photo_counts(self) -> dict:
        """{item_id: numero de fotos} en una sola pasada y sin copiar la hoja: el
        catalogo lo consulta en cada tarjeta."""
        df = _load_cache()["item_photos"]
        return {str(item_id): int(count) for item_id, count in df["item_id"].value_counts().items()}

    def delete_item_photo(self, photo_id: str, trace_event: dict = None):
        """Quita una foto del indice (y registra su evento) en una sola escritura.
        Devuelve la fila eliminada o None si no existia."""
        event_row = _trace_event_row(trace_event) if trace_event else None
        with _db_write():
            dfs = _read_excel()
            df = dfs["item_photos"]
            rows = df[df["id"] == photo_id]
            if rows.empty:
                return None
            removed = _clean_nan(rows.iloc[0].to_dict())
            dfs["item_photos"] = df[df["id"] != photo_id].reset_index(drop=True)
            if event_row:
                dfs["trace_events"] = pd.concat([dfs["trace_events"], pd.DataFrame([event_row])], ignore_index=True)
            _write_and_sync(dfs)
        return removed


def _admin_audit_row(actor: dict, event_type: str, details: dict) -> dict:
    """Fila de ``trace_events`` para una acción administrativa de ``actor`` (lista blanca,
    conflicto de sincronización…): quién, qué y cuándo, sin solicitud asociada."""
    return _trace_event_row({
        "event_type": event_type, "user_id": actor.get("id", ""), "owner_id": actor.get("id", ""),
        "actor_id": actor.get("id", ""), "actor_name": actor.get("full_name", ""),
        "actor_email": actor.get("institutional_email", ""), "actor_role": actor.get("role", ""),
        "details": json.dumps(details, ensure_ascii=False),
    })


def _trace_event_row(event: dict) -> dict:
    """Fila de `trace_events` con las mismas reglas que LabStorage.add_trace_event."""
    columns = SHEET_COLUMNS["trace_events"]
    row = {column: "" for column in columns}
    row.update({key: "" if value is None else str(value) for key, value in event.items() if key in columns})
    row["owner_id"] = row["owner_id"] or row["user_id"]
    row["user_id"] = row["user_id"] or row["owner_id"]
    row["id"] = _new_id()
    row["created_at"] = row["created_at"] or _now_str()
    return row
