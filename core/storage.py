# -*- coding: utf-8 -*-
"""
core/storage.py - Capa de base de datos: Excel local + sincronizacion automatica a GitHub.
Cache en memoria RAM y sincronizacion asincrona (hilo de fondo) para no bloquear la UI.
"""

import base64
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from threading import Lock

import pandas as pd
import requests
import streamlit as st
from streamlit.runtime.scriptrunner import add_script_run_ctx

from core import barcode
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
    "item_history": [
        "id", "item_id", "timestamp", "type", "quantity_change",
        "actor_user_id", "details",
    ],
}

_cached_dfs = None
_excel_lock = Lock()


# ---------------------------------------------------------------------------
# Sincronizacion con GitHub
# ---------------------------------------------------------------------------
# El push real corre en un hilo de fondo (ver _write_and_sync), asi que un
# st.toast/st.warning lanzado desde ahi puede perderse (el rerun principal ya
# termino de renderizar). Este estado se guarda aparte para que la UI, en el
# hilo principal de cada rerun, pueda mostrar de forma confiable si la ultima
# sincronizacion funciono o no (ver LabStorage.get_sync_status()).

_sync_status_lock = Lock()
_sync_status = {
    "configured": False,   # hay GITHUB_TOKEN + GITHUB_REPO en los Secrets
    "ok": None,             # None = todavia no se intento ninguna operacion
    "message": "",
    "last_op": None,        # "pull" | "push"
    "last_at": None,
    "repo": "",
    "db_path": "",
}


def _set_sync_status(configured: bool, ok, message: str, op: str = None) -> None:
    with _sync_status_lock:
        _sync_status["configured"] = configured
        _sync_status["ok"] = ok
        _sync_status["message"] = message
        _sync_status["last_op"] = op
        _sync_status["last_at"] = datetime.now(timezone.utc).isoformat()
        _sync_status["repo"] = safe_secret("GITHUB_REPO", "")
        _sync_status["db_path"] = safe_secret("GITHUB_DB_PATH", EXCEL_PATH)


def get_sync_status() -> dict:
    with _sync_status_lock:
        return dict(_sync_status)


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


def _github_pull() -> None:
    if not _is_github_configured():
        return
    try:
        url = _github_api_url()
        logger.info(f"GitHub pull -> {url}")
        resp = requests.get(url, headers=_github_headers(), timeout=30)

        if resp.status_code == 404:
            logger.info(f"{EXCEL_PATH} no existe en GitHub todavia. Se creara al primer guardado.")
            _set_sync_status(
                configured=True, ok=True,
                message="Conectado. La base de datos aun no existe en GitHub; se creara al primer guardado.",
                op="pull",
            )
            return

        if resp.status_code != 200:
            msg = _describe_github_error(resp.status_code, resp.text)
            logger.error(f"GitHub pull fallo: HTTP {resp.status_code} - {resp.text[:300]}")
            st.warning(f"No se pudo sincronizar con GitHub: {msg}")
            _set_sync_status(configured=True, ok=False, message=msg, op="pull")
            return

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
            return

        with open(EXCEL_PATH, "wb") as f:
            f.write(file_bytes)
        logger.info(f"{EXCEL_PATH} descargado desde GitHub ({len(file_bytes) / 1024:.1f} KB).")
        _set_sync_status(
            configured=True, ok=True,
            message=f"Ultima descarga OK ({len(file_bytes) / 1024:.1f} KB).", op="pull",
        )

    except Exception as e:
        logger.error(f"GitHub pull error: {e}")
        st.warning(f"No se pudo descargar la base de datos desde GitHub: {e}")
        _set_sync_status(configured=True, ok=False, message=str(e), op="pull")


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


def _github_push() -> None:
    if not _is_github_configured():
        return
    try:
        url = _github_api_url()
        headers = _github_headers()

        with open(EXCEL_PATH, "rb") as f:
            raw_bytes = f.read()
        content_b64 = base64.b64encode(raw_bytes).decode("utf-8")

        sha = ""
        get_resp = requests.get(url, headers=headers, timeout=15)
        if get_resp.status_code == 200:
            sha = get_resp.json().get("sha", "")
        elif get_resp.status_code != 404:
            logger.warning(f"No se pudo obtener SHA: HTTP {get_resp.status_code} - {get_resp.text[:200]}")

        commit_msg = f"Auto-sync {EXCEL_PATH} [{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC]"
        payload = {"message": commit_msg, "content": content_b64}
        if sha:
            payload["sha"] = sha

        put_resp = requests.put(url, headers=headers, json=payload, timeout=60)
        if put_resp.status_code in (200, 201):
            logger.info("Base de datos sincronizada con GitHub correctamente.")
            st.toast("Datos guardados y sincronizados con GitHub", icon="✅")
            _set_sync_status(configured=True, ok=True, message="Ultima sincronizacion OK.", op="push")
        else:
            msg = _describe_github_error(put_resp.status_code, put_resp.text)
            logger.error(f"GitHub push fallo: HTTP {put_resp.status_code} - {put_resp.text[:500]}")
            st.warning(f"Error de sincronizacion con GitHub: {msg}")
            _set_sync_status(configured=True, ok=False, message=msg, op="push")

    except FileNotFoundError:
        logger.error(f"No se encontro el archivo local {EXCEL_PATH} para subir a GitHub.")
        _set_sync_status(
            configured=True, ok=False,
            message=f"No se encontro el archivo local {EXCEL_PATH} para subir.", op="push",
        )
    except Exception as e:
        logger.error(f"GitHub push error inesperado: {e}")
        st.warning(f"Error inesperado al sincronizar con GitHub: {e}")
        _set_sync_status(configured=True, ok=False, message=str(e), op="push")


# ---------------------------------------------------------------------------
# Helpers internos de bajo nivel
# ---------------------------------------------------------------------------

def _read_excel() -> dict:
    global _cached_dfs
    if _cached_dfs is not None:
        return {k: v.copy() for k, v in _cached_dfs.items()}

    try:
        xls = pd.ExcelFile(EXCEL_PATH, engine="openpyxl")
        dfs = {}
        for sheet, cols in SHEET_COLUMNS.items():
            if sheet in xls.sheet_names:
                df = xls.parse(sheet, dtype=str)
                for col in cols:
                    if col not in df.columns:
                        df[col] = ""
                dfs[sheet] = df[cols]
            else:
                dfs[sheet] = pd.DataFrame(columns=cols)
        _cached_dfs = {k: v.copy() for k, v in dfs.items()}
        return dfs
    except FileNotFoundError:
        dfs = {sheet: pd.DataFrame(columns=cols) for sheet, cols in SHEET_COLUMNS.items()}
        _cached_dfs = {k: v.copy() for k, v in dfs.items()}
        return dfs


def _write_excel(dfs: dict) -> None:
    global _cached_dfs
    _cached_dfs = {k: v.copy() for k, v in dfs.items()}
    with pd.ExcelWriter(EXCEL_PATH, engine="openpyxl", mode="w") as writer:
        for sheet, df in dfs.items():
            df.to_excel(writer, sheet_name=sheet, index=False)


def _write_and_sync(dfs: dict) -> None:
    _write_excel(dfs)
    if _is_github_configured():
        t = threading.Thread(target=_github_push, daemon=True)
        add_script_run_ctx(t)
        t.start()


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


VALID_ITEM_TYPES = ("master", "child", "standalone")


def _validate_item_data(data: dict, df_items: pd.DataFrame, custom_id: str, is_new: bool = False) -> None:
    item_type = data.get("item_type", "standalone")
    parent_id = (data.get("parent_id") or "").strip()

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


def firestore_retry(func):
    def wrapper(*args, **kwargs):
        max_retries = 3
        delay = 1
        last_exception = None
        for attempt in range(max_retries):
            try:
                return func(*args, **kwargs)
            except ValueError:
                # Error de validacion (datos invalidos): no es transitorio,
                # reintentar no lo va a arreglar. Propagar de inmediato para
                # que la UI muestre el mensaje real en vez de agotar reintentos.
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
            dfs = {sheet: pd.DataFrame(columns=cols) for sheet, cols in SHEET_COLUMNS.items()}
            _write_and_sync(dfs)
        else:
            try:
                existing_sheets = pd.ExcelFile(EXCEL_PATH, engine="openpyxl").sheet_names
                if set(SHEET_COLUMNS.keys()) - set(existing_sheets):
                    dfs = _read_excel()
                    _write_excel(dfs)
            except Exception:
                pass

    def is_github_sync_active(self) -> bool:
        return _is_github_configured()

    def get_sync_status(self) -> dict:
        """Estado de la ultima sincronizacion con GitHub (ver core.storage.get_sync_status).
        Usado por la UI para avisar de forma visible si los datos NO se estan
        guardando de forma permanente."""
        return get_sync_status()

    # ------------------------------------------------------------------
    # ITEMS (productos, contenedores maestros e items hijos)
    # ------------------------------------------------------------------

    @firestore_retry
    def save_item(self, data: dict, custom_id: str, is_new: bool = False, actor_email: str = "", details: str = None):
        with _excel_lock:
            dfs = _read_excel()
            df_items = dfs["items"]
            df_hist = dfs["item_history"]

            idx = df_items.index[df_items["id"] == custom_id].tolist()
            existing = df_items.loc[idx[0]] if idx else None

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
        with _excel_lock:
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
                is_new = df_items[df_items["id"] == custom_id].empty
                try:
                    _validate_item_data(
                        {"item_type": item_type, "parent_id": parent_id}, df_items, custom_id, is_new=is_new
                    )
                except ValueError as e:
                    errors.append(f"'{custom_id}': {e}")
                    continue
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
        with _excel_lock:
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

    @firestore_retry
    def get_item(self, item_id: str):
        dfs = _read_excel()
        rows = dfs["items"][dfs["items"]["id"] == item_id]
        if rows.empty:
            return None
        return _row_to_item(rows.iloc[0])

    @firestore_retry
    def get_all_items(self, include_retired: bool = False) -> list:
        dfs = _read_excel()
        df = dfs["items"]
        items = [_row_to_item(row) for _, row in df.iterrows()]
        if not include_retired:
            items = [i for i in items if i.get("status") != "retired"]
        return sorted(items, key=lambda x: (x.get("name") or "").lower())

    @firestore_retry
    def get_children(self, parent_id: str) -> list:
        dfs = _read_excel()
        df = dfs["items"]
        rows = df[(df["parent_id"] == parent_id) & (df["status"] != "retired")]
        items = [_row_to_item(row) for _, row in rows.iterrows()]
        return sorted(items, key=lambda x: (x.get("name") or "").lower())

    @firestore_retry
    def get_all_masters(self) -> list:
        dfs = _read_excel()
        df = dfs["items"]
        rows = df[(df["item_type"] == "master") & (df["status"] != "retired")]
        items = [_row_to_item(row) for _, row in rows.iterrows()]
        return sorted(items, key=lambda x: (x.get("name") or "").lower())

    @firestore_retry
    def get_available_quantity(self, item_id: str) -> int:
        item = self.get_item(item_id)
        if not item:
            return 0
        total = item.get("quantity", 0)
        open_loans = self.get_open_loans_for_item(item_id)
        borrowed = sum(l.get("quantity", 0) for l in open_loans)
        return max(total - borrowed, 0)

    # ------------------------------------------------------------------
    # USUARIOS
    # ------------------------------------------------------------------

    @firestore_retry
    def get_user_by_email(self, email: str):
        dfs = _read_excel()
        df = dfs["users"]
        rows = df[df["institutional_email"] == email.lower().strip()]
        if rows.empty:
            return None
        return _row_to_user(rows.iloc[0])

    @firestore_retry
    def get_user_by_id(self, user_id: str):
        dfs = _read_excel()
        df = dfs["users"]
        rows = df[df["id"] == user_id]
        if rows.empty:
            return None
        return _row_to_user(rows.iloc[0])

    def create_user(self, full_name: str, email: str, password_hash: str, role: str, program: str = "", student_id: str = "", status: str = "active") -> dict:
        with _excel_lock:
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
        with _excel_lock:
            dfs = _read_excel()
            df = dfs["users"]
            idx = df.index[df["id"] == user_id].tolist()
            if idx:
                for k, v in changes.items():
                    df.at[idx[0], k] = v
                dfs["users"] = df
                _write_and_sync(dfs)

    @firestore_retry
    def get_all_users(self) -> list:
        dfs = _read_excel()
        df = dfs["users"]
        users = [_row_to_user(row) for _, row in df.iterrows()]
        return sorted(users, key=lambda x: (x.get("full_name") or "").lower())

    def count_users(self) -> int:
        dfs = _read_excel()
        return len(dfs["users"])

    def count_masters(self) -> int:
        dfs = _read_excel()
        df = dfs["users"]
        return len(df[df["role"] == "maestro"])

    # ------------------------------------------------------------------
    # LISTA BLANCA DE PROFESORES
    # ------------------------------------------------------------------

    @firestore_retry
    def is_email_whitelisted_professor(self, email: str) -> bool:
        dfs = _read_excel()
        df = dfs["professors_whitelist"]
        return email.lower().strip() in set(df["institutional_email"].str.lower().str.strip())

    @firestore_retry
    def get_whitelist(self) -> list:
        dfs = _read_excel()
        df = dfs["professors_whitelist"]
        return sorted(df["institutional_email"].tolist())

    def add_to_whitelist(self, email: str):
        with _excel_lock:
            dfs = _read_excel()
            df = dfs["professors_whitelist"]
            email = email.lower().strip()
            if email not in set(df["institutional_email"]):
                dfs["professors_whitelist"] = pd.concat(
                    [df, pd.DataFrame([{"institutional_email": email}])], ignore_index=True
                )
                _write_and_sync(dfs)

    def remove_from_whitelist(self, email: str):
        with _excel_lock:
            dfs = _read_excel()
            df = dfs["professors_whitelist"]
            dfs["professors_whitelist"] = df[df["institutional_email"] != email.lower().strip()]
            _write_and_sync(dfs)

    # ------------------------------------------------------------------
    # PRESTAMOS (loans)
    # ------------------------------------------------------------------

    def create_loan(self, item: dict, quantity: int, user: dict, expected_return_at=None, notes: str = "") -> dict:
        with _excel_lock:
            dfs = _read_excel()
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
        with _excel_lock:
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
        dfs = _read_excel()
        df = dfs["loans"]
        rows = df[(df["item_id"] == item_id) & (df["status"] == "out")]
        return [_row_to_loan(row) for _, row in rows.iterrows()]

    @firestore_retry
    def get_open_loans_for_user(self, user_id: str) -> list:
        dfs = _read_excel()
        df = dfs["loans"]
        rows = df[(df["user_id"] == user_id) & (df["status"] == "out")]
        loans = [_row_to_loan(row) for _, row in rows.iterrows()]
        return sorted(loans, key=lambda x: x.get("checkout_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    @firestore_retry
    def get_all_loans(self, status: str = None) -> list:
        dfs = _read_excel()
        df = dfs["loans"]
        if df.empty:
            return []
        if status:
            df = df[df["status"] == status]
        loans = [_row_to_loan(row) for _, row in df.iterrows()]
        return sorted(loans, key=lambda x: x.get("checkout_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    @firestore_retry
    def get_item_history(self, item_id: str) -> list:
        dfs = _read_excel()
        df = dfs["item_history"]
        rows = df[df["item_id"] == item_id]
        history = [_clean_nan(row.to_dict()) for _, row in rows.iterrows()]
        return sorted(history, key=lambda x: x.get("timestamp") or "", reverse=True)
