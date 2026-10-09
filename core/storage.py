# -*- coding: utf-8 -*-
"""
core/storage.py - Capa de base de datos: Excel local + sincronizacion automatica a GitHub.
Cache en memoria RAM, escritura atomica y publicacion sincrona y serializada en GitHub.
"""

import base64
import io
import logging
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Lock

import pandas as pd
import requests
import streamlit as st

from core import barcode
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
    ],
    "users": [
        "id", "full_name", "student_id", "institutional_email", "password_hash", "role",
        "program_or_department", "status", "created_at",
    ],
    "professors_whitelist": [
        "institutional_email",
    ],
    "loans": [
        "id", "item_id", "item_name", "parent_id", "quantity", "user_id",
        "user_name", "user_role", "checkout_at", "expected_return_at",
        "return_at", "status", "notes",
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
        "id", "event_type", "request_id", "item_id", "loan_id", "user_id",
        "actor_id", "actor_name", "actor_email", "receipt", "details", "created_at",
    ],
}

_cached_dfs = None
_excel_lock = Lock()


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
_sync_state = {"dirty": False, "remote_sha": None}

_TRANSIENT_STATUS = (429, 500, 502, 503, 504)


def _reset_sync_state() -> None:
    """Restablece el estado de sincronizacion (uso en pruebas)."""
    with _sync_status_lock:
        _sync_status.update(
            configured=False, ok=None, message="", last_op=None, last_at=None,
            repo="", db_path="", conflict=False,
        )
    with _sync_state_lock:
        _sync_state.update(dirty=False, remote_sha=None)


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
        return dict(_sync_status)


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


def _github_request(method: str, url: str, attempts: int = 3, **kwargs):
    """Llamada a la API de GitHub con reintentos ante fallos transitorios de red
    o respuestas 429/5xx (esperas de 1 s y 2 s). Los demas codigos HTTP se
    devuelven tal cual para que el llamador los interprete."""
    last_exc = None
    for attempt in range(attempts):
        try:
            resp = requests.request(method, url, headers=_github_headers(), **kwargs)
        except requests.RequestException as exc:
            last_exc = exc
        else:
            if resp.status_code not in _TRANSIENT_STATUS or attempt == attempts - 1:
                return resp
        if attempt < attempts - 1:
            time.sleep(2 ** attempt)
    raise last_exc


def _atomic_write_bytes(path: str, data: bytes) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


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
            msg = _describe_github_error(resp.status_code, resp.text)
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

        _atomic_write_bytes(EXCEL_PATH, file_bytes)
        _cached_dfs = None
        _set_remote_sha(data.get("sha"))
        _clear_dirty()
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


def _describe_github_error(status_code: int, body: str) -> str:
    """Traduce los codigos de error mas comunes de la API de GitHub a un
    mensaje accionable (en vez de solo el HTTP crudo)."""
    if status_code == 404:
        repo = safe_secret("GITHUB_REPO", "")
        return (
            f"El repositorio '{repo}' no existe o el token no tiene acceso a el. "
            "Verifica GITHUB_REPO en los Secrets de Streamlit (Settings -> Secrets)."
        )
    if status_code in (401, 403):
        return (
            "El GITHUB_TOKEN no es valido o no tiene permiso de escritura "
            "('Contents: Read and write') sobre el repositorio configurado."
        )
    return f"HTTP {status_code} - {body[:300]}"


_CONFLICT_MESSAGE = (
    "La base de datos en GitHub es distinta de la que esta app descargo (alguien la edito "
    "fuera de la app, hay otra instancia escribiendo, o aun no se logro descargarla). "
    "Para no pisar esos datos, los cambios recientes quedaron SOLO en este contenedor. "
    "El perfil maestro debe elegir que version conservar."
)


def _github_push(force: bool = False) -> bool:
    """Publica el Excel local en GitHub. Nunca lanza; devuelve si se publico.

    Sin `force`, rechaza el push (y marca conflicto) si GitHub tiene una version
    que esta app no conoce. Con `force=True` la version local gana."""
    if not _is_github_configured():
        return False
    try:
        url = _github_api_url()

        with open(EXCEL_PATH, "rb") as f:
            raw_bytes = f.read()
        content_b64 = base64.b64encode(raw_bytes).decode("utf-8")

        get_resp = _github_request("GET", url, timeout=15)
        if get_resp.status_code == 200:
            remote_sha = get_resp.json().get("sha", "")
        elif get_resp.status_code == 404:
            remote_sha = ""
        else:
            msg = _describe_github_error(get_resp.status_code, get_resp.text)
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

        if put_resp.status_code in (409, 422) and not force:
            # Alguien publico entre nuestra lectura del SHA y el PUT.
            _set_sync_status(
                configured=True, ok=False, message=_CONFLICT_MESSAGE, op="push", conflict=True
            )
            return False

        msg = _describe_github_error(put_resp.status_code, put_resp.text)
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


def _flush_pending_push() -> None:
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
    if _cached_dfs is None:
        try:
            xls = pd.ExcelFile(EXCEL_PATH, engine="openpyxl")
            dfs = {}
            for sheet, cols in SHEET_COLUMNS.items():
                if sheet in xls.sheet_names:
                    # keep_default_na=False + fillna: las celdas vacias llegan como "" (no
                    # como NaN, que rompia .strip() al editar items tras recargar el Excel)
                    # y un texto legitimo como "NA" o "None" ya no se convierte en nulo.
                    df = xls.parse(sheet, dtype=str, keep_default_na=False).fillna("")
                    for col in cols:
                        if col not in df.columns:
                            df[col] = ""
                    dfs[sheet] = df[cols]
                else:
                    dfs[sheet] = pd.DataFrame(columns=cols)
        except FileNotFoundError:
            dfs = {sheet: pd.DataFrame(columns=cols) for sheet, cols in SHEET_COLUMNS.items()}
        _cached_dfs = dfs
    return _cached_dfs


def _read_excel(*sheets: str) -> dict:
    """Copias de las hojas pedidas (todas si no se indica ninguna). Los lectores
    piden solo lo que usan para no copiar la base completa en cada consulta."""
    cache = _load_cache()
    names = sheets or tuple(cache)
    return {name: cache[name].copy() for name in names}


def _write_excel(dfs: dict) -> None:
    """Escribe el Excel de forma atomica: o queda la version anterior completa
    o la nueva completa, nunca un archivo a medias."""
    global _cached_dfs
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for sheet, df in dfs.items():
            df.to_excel(writer, sheet_name=sheet, index=False)
    try:
        _atomic_write_bytes(EXCEL_PATH, buffer.getvalue())
    except Exception:
        try:
            os.remove(f"{EXCEL_PATH}.tmp")
        except OSError:
            pass
        raise
    _cached_dfs = {k: v.copy() for k, v in dfs.items()}


def _write_and_sync(dfs: dict) -> None:
    """Guarda en disco y marca la base como pendiente de publicar. La
    publicacion en GitHub ocurre al cerrar la seccion `_db_write`."""
    _write_excel(dfs)
    _mark_dirty()


def _new_id() -> str:
    return uuid.uuid4().hex[:20]


def _now_str() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_nan(d: dict) -> dict:
    for k, v in d.items():
        if str(v) in ("nan", "None", "NaT"):
            d[k] = None
    return d


def _row_to_item(row: pd.Series) -> dict:
    item = row.to_dict()
    for num_col in ("quantity", "min_stock_alert"):
        try:
            v = item.get(num_col)
            item[num_col] = int(float(v)) if v not in (None, "", "nan", "None") else 0
        except Exception:
            item[num_col] = 0
    return _clean_nan(item)


def _row_to_user(row: pd.Series) -> dict:
    return _clean_nan(row.to_dict())


def _row_to_loan(row: pd.Series) -> dict:
    loan = row.to_dict()
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


def _row_to_reservation(row: pd.Series) -> dict:
    record = _clean_nan(row.to_dict())
    try:
        record["attendees"] = int(float(record.get("attendees") or 0))
    except (TypeError, ValueError):
        record["attendees"] = 0
    record["email_notified"] = str(record.get("email_notified") or "").lower() in ("true", "1")
    return _parse_record_dates(
        record, ("start_at", "end_at", "reviewed_at", "created_at", "updated_at")
    )


def _row_to_service_request(row: pd.Series) -> dict:
    record = _clean_nan(row.to_dict())
    try:
        record["quantity"] = int(float(record.get("quantity") or 0))
    except (TypeError, ValueError):
        record["quantity"] = 0
    record["email_notified"] = str(record.get("email_notified") or "").lower() in ("true", "1")
    return _parse_record_dates(
        record, ("needed_at", "reviewed_at", "created_at", "updated_at")
    )


VALID_ITEM_TYPES = ("master", "child", "standalone")


def _validate_item_data(data: dict, df_items: pd.DataFrame, custom_id: str, is_new: bool = False) -> None:
    item_type = data.get("item_type", "standalone")
    parent_id = str(data.get("parent_id") or "").strip()
    if parent_id.lower() in ("nan", "none"):  # celdas nulas heredadas de Excel
        parent_id = ""

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
_NON_RETRYABLE = (ValueError, KeyError, TypeError, AttributeError)


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

    if open_requests.any():
        now = _now_str()
        for column, value in {
            "status": "cancelled", "reviewed_by": actor_email, "reviewed_at": now,
            "review_notes": "Producto eliminado del inventario.", "updated_at": now,
        }.items():
            df_req.loc[open_requests, column] = value
        dfs["service_requests"] = df_req
    return counts


def _available_from(dfs: dict, item_id: str):
    """Disponibilidad calculada sobre `dfs` (items + loans). None si el item no existe."""
    rows = dfs["items"][dfs["items"]["id"] == item_id]
    if rows.empty:
        return None
    total = _row_to_item(rows.iloc[0]).get("quantity", 0)
    loans = dfs["loans"]
    open_loans = loans[(loans["item_id"] == item_id) & (loans["status"] == "out")]
    borrowed = sum(_row_to_loan(row).get("quantity", 0) for _, row in open_loans.iterrows())
    return max(total - borrowed, 0)


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
            logger.info(f"Creando archivo Excel nuevo: {EXCEL_PATH}")
            with _db_write():
                dfs = {sheet: pd.DataFrame(columns=cols) for sheet, cols in SHEET_COLUMNS.items()}
                _write_and_sync(dfs)
        else:
            try:
                existing_sheets = pd.ExcelFile(EXCEL_PATH, engine="openpyxl").sheet_names
                if set(SHEET_COLUMNS.keys()) - set(existing_sheets):
                    with _excel_lock:
                        _write_excel(_read_excel())
            except Exception:
                pass

    def is_github_sync_active(self) -> bool:
        return _is_github_configured()

    def get_sync_status(self) -> dict:
        """Estado de la ultima sincronizacion con GitHub (ver core.storage.get_sync_status).
        Usado por la UI para avisar de forma visible si los datos NO se estan
        guardando de forma permanente."""
        return get_sync_status()

    def retry_sync(self) -> bool:
        """Reintenta publicar ahora la version local (sin forzar). Devuelve si
        la ultima sincronizacion quedo sin errores."""
        _mark_dirty()
        _flush_pending_push()
        return get_sync_status()["ok"] is not False

    def resolve_sync_conflict(self, keep: str):
        """Resuelve un conflicto de sincronizacion. Devuelve (ok, mensaje).

        keep="remote": descarta lo local y adopta la version de GitHub.
        keep="local":  publica la version local sobre la de GitHub (la remota
                       anterior sigue disponible en el historial de commits)."""
        if keep == "remote":
            with _excel_lock:
                replaced = _github_pull()
            if replaced:
                return True, "Se cargo la version de GitHub. Los cambios locales sin publicar se descartaron."
            return False, get_sync_status()["message"] or "No hay una version remota que descargar."
        if keep == "local":
            with _push_lock:
                published = _github_push(force=True)
                if published:
                    _clear_dirty()
            if published:
                return True, "Se publico la version local en GitHub."
            return False, get_sync_status()["message"]
        raise ValueError("keep debe ser 'remote' o 'local'.")

    # ------------------------------------------------------------------
    # ITEMS (productos, contenedores maestros e items hijos)
    # ------------------------------------------------------------------

    @firestore_retry
    def save_item(self, data: dict, custom_id: str, is_new: bool = False, actor_email: str = "", details: str = None):
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
                "min_stock_alert": data.get("min_stock_alert", 0),
                "status": data.get("status", "active"),
                "created_by": data.get("created_by", actor_email),
                "updated_at": _now_str(),
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

    def bulk_upsert_items(self, rows: list, actor_email: str = "") -> dict:
        """Crea/actualiza muchos items en UNA sola lectura+escritura+sync
        (evita decenas de commits a GitHub en una importacion masiva)."""
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
                row = {
                    "id": custom_id,
                    "name": name,
                    "category": str(raw.get("category", "") or ""),
                    "description": str(raw.get("description", "") or ""),
                    "item_type": item_type,
                    "parent_id": parent_id,
                    "unit": str(raw.get("unit", "unidad") or "unidad"),
                    "quantity": int(float(raw.get("quantity", 0) or 0)),
                    "location": str(raw.get("location", "") or ""),
                    "min_stock_alert": int(float(raw.get("min_stock_alert", 0) or 0)),
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

    def retire_item(self, item_id: str, actor_email: str = ""):
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

    def delete_item(self, item_id: str, actor_email: str = ""):
        """Elimina DEFINITIVAMENTE un item de la base de datos. Devuelve (ok, mensaje).

        A diferencia de `retire_item` (baja logica que conserva la fila), aqui
        desaparece todo: el item, su historial y sus prestamos devueltos. Si es un
        Contenedor Principal tambien se eliminan los items que contiene. Las
        solicitudes pendientes o aprobadas del producto se cancelan. Se bloquea si
        hay prestamos abiertos. El codigo queda libre para volver a usarse."""
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
        dfs = _read_excel("items", "loans", "service_requests")
        df_items, df_loans, df_req = dfs["items"], dfs["loans"], dfs["service_requests"]
        if df_items[df_items["id"] == item_id].empty:
            return {"exists": False, "contained_items": 0, "open_loans": 0,
                    "closed_loans": 0, "open_requests": 0}
        ids = _cascade_ids(df_items, item_id)
        loans = df_loans[df_loans["item_id"].isin(ids)]
        return {
            "exists": True,
            "contained_items": len(ids) - 1,
            "open_loans": int((loans["status"] == "out").sum()),
            "closed_loans": int((loans["status"] != "out").sum()),
            "open_requests": int(
                (df_req["item_id"].isin(ids) & df_req["status"].isin(("pending", "approved"))).sum()
            ),
        }

    @firestore_retry
    def item_code_in_use(self, code: str) -> bool:
        """True si `code` pertenece a un item que NO esta dado de baja. Un codigo
        de un item dado de baja se considera libre y puede volver a registrarse."""
        item = self.get_item((code or "").strip())
        return bool(item) and item.get("status") != "retired"

    @firestore_retry
    def get_item(self, item_id: str):
        dfs = _read_excel("items")
        rows = dfs["items"][dfs["items"]["id"] == item_id]
        if rows.empty:
            return None
        return _row_to_item(rows.iloc[0])

    @firestore_retry
    def get_all_items(self, include_retired: bool = False) -> list:
        dfs = _read_excel("items")
        df = dfs["items"]
        items = [_row_to_item(row) for _, row in df.iterrows()]
        if not include_retired:
            items = [i for i in items if i.get("status") != "retired"]
        return sorted(items, key=lambda x: (x.get("name") or "").lower())

    @firestore_retry
    def get_children(self, parent_id: str) -> list:
        dfs = _read_excel("items")
        df = dfs["items"]
        rows = df[(df["parent_id"] == parent_id) & (df["status"] != "retired")]
        items = [_row_to_item(row) for _, row in rows.iterrows()]
        return sorted(items, key=lambda x: (x.get("name") or "").lower())

    @firestore_retry
    def get_all_masters(self) -> list:
        dfs = _read_excel("items")
        df = dfs["items"]
        rows = df[(df["item_type"] == "master") & (df["status"] != "retired")]
        items = [_row_to_item(row) for _, row in rows.iterrows()]
        return sorted(items, key=lambda x: (x.get("name") or "").lower())

    @firestore_retry
    def get_available_quantity(self, item_id: str) -> int:
        available = _available_from(_read_excel("items", "loans"), item_id)
        return 0 if available is None else available

    @firestore_retry
    def get_availability_map(self) -> dict:
        """Disponibilidad de TODOS los items en una sola pasada: {id: disponible}.
        Evita una lectura de la base por cada item al listar el catalogo."""
        dfs = _read_excel("items", "loans")
        loans = dfs["loans"]
        borrowed = {}
        for _, row in loans[loans["status"] == "out"].iterrows():
            loan = _row_to_loan(row)
            borrowed[loan["item_id"]] = borrowed.get(loan["item_id"], 0) + loan.get("quantity", 0)
        availability = {}
        for _, row in dfs["items"].iterrows():
            item = _row_to_item(row)
            availability[item["id"]] = max(item.get("quantity", 0) - borrowed.get(item["id"], 0), 0)
        return availability

    # ------------------------------------------------------------------
    # USUARIOS
    # ------------------------------------------------------------------

    @firestore_retry
    def get_user_by_email(self, email: str):
        dfs = _read_excel("users")
        df = dfs["users"]
        rows = df[df["institutional_email"] == email.lower().strip()]
        if rows.empty:
            return None
        return _row_to_user(rows.iloc[0])

    @firestore_retry
    def get_user_by_id(self, user_id: str):
        dfs = _read_excel("users")
        df = dfs["users"]
        rows = df[df["id"] == user_id]
        if rows.empty:
            return None
        return _row_to_user(rows.iloc[0])

    def create_user(self, full_name: str, email: str, password_hash: str, role: str, program: str = "", student_id: str = "", status: str = "active") -> dict:
        with _db_write():
            dfs = _read_excel()
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
            }
            dfs["users"] = pd.concat([dfs["users"], pd.DataFrame([row])], ignore_index=True)
            _write_and_sync(dfs)
            return row

    def update_user(self, user_id: str, changes: dict):
        """Actualiza solo campos de usuario conocidos y devuelve el registro.

        La vista valida semántica (nombre, correo, programa); esta capa evita
        columnas arbitrarias y garantiza unicidad de correo para cualquier cliente.
        """
        allowed = {
            "full_name", "student_id", "institutional_email", "password_hash",
            "role", "program_or_department", "status",
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
            if "institutional_email" in changes:
                email = str(changes["institutional_email"] or "").lower().strip()
                duplicate = df[(df["institutional_email"].str.lower().str.strip() == email) & (df["id"] != user_id)]
                if not duplicate.empty:
                    raise ValueError("Ya existe otra cuenta con ese correo institucional.")
                changes["institutional_email"] = email
            if "role" in changes and changes["role"] not in ("estudiante", "profesor", "maestro"):
                raise ValueError("Rol de usuario inválido.")
            if "status" in changes and changes["status"] not in ("active", "disabled"):
                raise ValueError("Estado de usuario inválido.")
            for key, value in changes.items():
                df.at[idx[0], key] = value
            dfs["users"] = df
            _write_and_sync(dfs)
            return _row_to_user(df.loc[idx[0]])

    @firestore_retry
    def get_all_users(self) -> list:
        dfs = _read_excel("users")
        df = dfs["users"]
        users = [_row_to_user(row) for _, row in df.iterrows()]
        return sorted(users, key=lambda x: (x.get("full_name") or "").lower())

    def count_users(self) -> int:
        dfs = _read_excel("users")
        return len(dfs["users"])

    def count_masters(self) -> int:
        dfs = _read_excel("users")
        df = dfs["users"]
        return len(df[df["role"] == "maestro"])

    # ------------------------------------------------------------------
    # LISTA BLANCA DE PROFESORES
    # ------------------------------------------------------------------

    @firestore_retry
    def is_email_whitelisted_professor(self, email: str) -> bool:
        dfs = _read_excel("professors_whitelist")
        df = dfs["professors_whitelist"]
        return email.lower().strip() in set(df["institutional_email"].str.lower().str.strip())

    @firestore_retry
    def get_whitelist(self) -> list:
        dfs = _read_excel("professors_whitelist")
        df = dfs["professors_whitelist"]
        return sorted(df["institutional_email"].tolist())

    def add_to_whitelist(self, email: str):
        with _db_write():
            dfs = _read_excel()
            df = dfs["professors_whitelist"]
            email = email.lower().strip()
            if email not in set(df["institutional_email"]):
                dfs["professors_whitelist"] = pd.concat(
                    [df, pd.DataFrame([{"institutional_email": email}])], ignore_index=True
                )
                _write_and_sync(dfs)

    def remove_from_whitelist(self, email: str):
        with _db_write():
            dfs = _read_excel()
            df = dfs["professors_whitelist"]
            dfs["professors_whitelist"] = df[df["institutional_email"] != email.lower().strip()]
            _write_and_sync(dfs)

    # ------------------------------------------------------------------
    # PRESTAMOS (loans)
    # ------------------------------------------------------------------

    def create_loan(self, item: dict, quantity: int, user: dict, expected_return_at=None, notes: str = "") -> dict:
        with _db_write():
            dfs = _read_excel()
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
            }
            dfs["loans"] = pd.concat([dfs["loans"], pd.DataFrame([row])], ignore_index=True)

            hist_row = {
                "id": _new_id(), "item_id": item["id"], "timestamp": _now_str(),
                "type": "Salida", "quantity_change": -quantity,
                "actor_user_id": user.get("institutional_email", ""),
                "details": f"Prestado a {user.get('full_name', '')} ({row['id']})",
            }
            dfs["item_history"] = pd.concat([dfs["item_history"], pd.DataFrame([hist_row])], ignore_index=True)
            _write_and_sync(dfs)
            return row

    def return_loan(self, loan_id: str, actor_email: str = ""):
        with _db_write():
            dfs = _read_excel()
            df = dfs["loans"]
            idx = df.index[df["id"] == loan_id].tolist()
            if not idx:
                return False, "El prestamo no existe."
            i = idx[0]
            if str(df.at[i, "return_at"]).strip() not in ("", "nan", "None"):
                return False, "Este prestamo ya fue devuelto."

            df.at[i, "return_at"] = _now_str()
            df.at[i, "status"] = "returned"
            item_id = df.at[i, "item_id"]
            qty = int(float(df.at[i, "quantity"] or 0))
            dfs["loans"] = df

            hist_row = {
                "id": _new_id(), "item_id": item_id, "timestamp": _now_str(),
                "type": "Reingreso", "quantity_change": qty,
                "actor_user_id": actor_email, "details": f"Reingreso de prestamo {loan_id}",
            }
            dfs["item_history"] = pd.concat([dfs["item_history"], pd.DataFrame([hist_row])], ignore_index=True)
            _write_and_sync(dfs)
            return True, "Reingreso registrado correctamente."

    @firestore_retry
    def get_open_loans_for_item(self, item_id: str) -> list:
        dfs = _read_excel("loans")
        df = dfs["loans"]
        rows = df[(df["item_id"] == item_id) & (df["status"] == "out")]
        return [_row_to_loan(row) for _, row in rows.iterrows()]

    @firestore_retry
    def get_open_loans_for_user(self, user_id: str) -> list:
        dfs = _read_excel("loans")
        df = dfs["loans"]
        rows = df[(df["user_id"] == user_id) & (df["status"] == "out")]
        loans = [_row_to_loan(row) for _, row in rows.iterrows()]
        return sorted(loans, key=lambda x: x.get("checkout_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    @firestore_retry
    def get_all_loans(self, status: str = None) -> list:
        dfs = _read_excel("loans")
        df = dfs["loans"]
        if df.empty:
            return []
        if status:
            df = df[df["status"] == status]
        loans = [_row_to_loan(row) for _, row in df.iterrows()]
        return sorted(loans, key=lambda x: x.get("checkout_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    @firestore_retry
    def get_item_history(self, item_id: str) -> list:
        dfs = _read_excel("item_history")
        df = dfs["item_history"]
        rows = df[df["item_id"] == item_id]
        history = [_clean_nan(row.to_dict()) for _, row in rows.iterrows()]
        return sorted(history, key=lambda x: x.get("timestamp") or "", reverse=True)
    # ------------------------------------------------------------------
    # RESERVAS DE LABORATORIO
    # ------------------------------------------------------------------

    @staticmethod
    def _approved_reservations(dfs: dict) -> list:
        df = dfs["reservations"]
        return [_row_to_reservation(row) for _, row in df[df["status"] == "approved"].iterrows()]

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
        dfs = _read_excel("reservations")
        rows = dfs["reservations"][dfs["reservations"]["id"] == reservation_id]
        return None if rows.empty else _row_to_reservation(rows.iloc[0])

    @firestore_retry
    def get_reservations(self, status: str = None, user_id: str = None) -> list:
        df = _read_excel("reservations")["reservations"]
        if status:
            df = df[df["status"] == status]
        if user_id:
            df = df[df["requester_id"] == user_id]
        rows = [_row_to_reservation(row) for _, row in df.iterrows()]
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

    def create_service_request(self, data: dict, user: dict) -> dict:
        with _db_write():
            dfs = _read_excel()
            now = _now_str()
            row = {
                "id": _new_id(), "request_type": data["request_type"],
                "item_id": data.get("item_id", ""), "item_name": data.get("item_name", ""),
                "quantity": int(data.get("quantity", 0)), "service_name": data.get("service_name", ""),
                "description": data.get("description", ""), "needed_at": data.get("needed_at", ""),
                "requester_id": user["id"], "requester_name": user.get("full_name", ""),
                "requester_email": user.get("institutional_email", ""), "status": "pending",
                "reviewed_by": "", "reviewed_at": "", "review_notes": "",
                "email_notified": False, "email_error": "", "created_at": now, "updated_at": now,
            }
            dfs["service_requests"] = pd.concat(
                [dfs["service_requests"], pd.DataFrame([row])], ignore_index=True
            )
            _write_and_sync(dfs)
            return row

    @firestore_retry
    def get_service_request(self, request_id: str):
        dfs = _read_excel("service_requests")
        rows = dfs["service_requests"][dfs["service_requests"]["id"] == request_id]
        return None if rows.empty else _row_to_service_request(rows.iloc[0])

    @firestore_retry
    def get_service_requests(self, status: str = None, user_id: str = None) -> list:
        df = _read_excel("service_requests")["service_requests"]
        if status:
            df = df[df["status"] == status]
        if user_id:
            df = df[df["requester_id"] == user_id]
        rows = [_row_to_service_request(row) for _, row in df.iterrows()]
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

    # ------------------------------------------------------------------
    # CONFIGURACION DEL LABORATORIO (clave / valor)
    # ------------------------------------------------------------------

    @firestore_retry
    def get_setting(self, key: str, default=None):
        df = _read_excel("settings")["settings"]
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
        """Agrega UN evento de trazabilidad (una escritura = un commit). Solo se
        guardan las columnas conocidas; `details` debe venir ya como texto JSON."""
        columns = SHEET_COLUMNS["trace_events"]
        row = {column: "" for column in columns}
        row.update({key: "" if value is None else str(value) for key, value in event.items() if key in columns})
        row["id"] = _new_id()
        row["created_at"] = row["created_at"] or _now_str()
        with _db_write():
            dfs = _read_excel()
            dfs["trace_events"] = pd.concat([dfs["trace_events"], pd.DataFrame([row])], ignore_index=True)
            _write_and_sync(dfs)
        return dict(row)

    @firestore_retry
    def get_trace_events(self, request_id: str = None, item_id: str = None, user_id: str = None,
                         event_type: str = None, loan_id: str = None, receipt: str = None) -> list:
        """Eventos que cumplen TODOS los filtros dados, del más antiguo al más reciente."""
        df = _read_excel("trace_events")["trace_events"]
        filters = {"request_id": request_id, "item_id": item_id, "user_id": user_id,
                   "event_type": event_type, "loan_id": loan_id, "receipt": receipt}
        for column, value in filters.items():
            if value is not None:
                df = df[df[column] == value]
        rows = [_clean_nan(row.to_dict()) for _, row in df.iterrows()]
        return sorted(rows, key=lambda row: row.get("created_at") or "")

    # ------------------------------------------------------------------
    # ALTA DE VARIOS ITEMS NUEVOS EN UNA SOLA ESCRITURA
    # ------------------------------------------------------------------

    @firestore_retry
    def save_items_bulk(self, items: list, actor_email: str = "", details: str = None) -> list:
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
