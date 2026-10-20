# -*- coding: utf-8 -*-
"""v1.16 · la descarga de GitHub se valida antes de reemplazar la copia local, y los
conflictos falsos o el límite de tasa de GitHub no se confunden con un conflicto real."""

import pytest
from test_sync import ACTOR, FakeGitHub, Resp

from core import storage as sm
from core.storage import LabStorage


@pytest.fixture
def github(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(sm.requests, "request", fake.request)
    monkeypatch.setattr(sm.time, "sleep", lambda _s: None)
    secrets = {"GITHUB_TOKEN": "token-de-prueba", "GITHUB_REPO": "org/repo-de-prueba"}
    monkeypatch.setattr(sm, "safe_secret", lambda key, default=None: secrets.get(key, default))
    return fake


def _add(db, code):
    db.save_item({"name": "Item", "item_type": "standalone", "quantity": 1}, code, is_new=True, actor_email=ACTOR)


def _local_bytes():
    with open(sm.EXCEL_PATH, "rb") as handle:
        return handle.read()


# --- B · una descarga dañada nunca reemplaza la copia buena -------------------------------------

@pytest.mark.parametrize("bad", [b"esto no es un excel", b"PK\x03\x04truncado"])
def test_invalid_download_is_discarded_and_the_local_copy_is_kept(github, bad):
    db = LabStorage()
    _add(db, "LAB-001")
    good = _local_bytes()
    github.set_external(bad)

    assert sm._github_pull() is False
    assert _local_bytes() == good                                  # la copia buena sigue intacta
    status = sm.get_sync_status()
    assert status["ok"] is False and "se descartó" in status["message"]
    sm._cached_dfs = None
    assert db.get_item("LAB-001") is not None


def test_empty_download_keeps_the_local_copy(github):
    db = LabStorage()
    _add(db, "LAB-001")
    good = _local_bytes()
    github.set_external(b"")

    assert sm._github_pull() is False
    assert _local_bytes() == good and sm.get_sync_status()["ok"] is False


def test_workbook_without_any_database_sheet_is_discarded(github, tmp_path):
    import pandas as pd

    db = LabStorage()
    _add(db, "LAB-001")
    good = _local_bytes()
    other = tmp_path / "otro.xlsx"
    pd.DataFrame({"a": [1]}).to_excel(other, sheet_name="Hoja1", index=False)
    github.set_external(other.read_bytes())

    assert sm._github_pull() is False
    assert _local_bytes() == good
    assert "ninguna hoja de la base" in sm.get_sync_status()["message"]


def test_valid_download_replaces_the_local_copy_and_keeps_a_backup(github):
    db = LabStorage()
    _add(db, "LAB-001")
    before = _local_bytes()
    _add(db, "LAB-002")
    remote_v2 = github.content
    github.set_external(before)                  # GitHub vuelve a la versión anterior (válida)
    assert sm._github_pull() is True

    assert _local_bytes() == before
    with open(f"{sm.EXCEL_PATH}.bak", "rb") as handle:   # lo que había antes del reemplazo
        assert handle.read() == remote_v2


def test_conflict_resolution_keeping_remote_refuses_a_broken_remote(github):
    db = LabStorage()
    _add(db, "LAB-001")
    github.set_external(b"roto")
    ok, message = db.resolve_sync_conflict("remote")
    assert ok is False and "se descartó" in message
    assert db.get_item("LAB-001") is not None


def test_app_shows_a_retry_screen_instead_of_an_empty_base_when_github_is_down(monkeypatch):
    from pathlib import Path

    import streamlit as st
    from streamlit.testing.v1 import AppTest

    def boom(self):
        raise sm.RemoteUnavailableError("No se pudo descargar la base de datos desde GitHub y no hay una copia local.")

    st.cache_resource.clear()
    monkeypatch.setattr(sm.LabStorage, "__init__", boom)
    app = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"), default_timeout=30).run()
    assert not app.exception
    assert any("No se pudo cargar la base de datos" in error.value for error in app.error)
    assert any("Reintentar" in button.label for button in app.button)
    st.cache_resource.clear()


# --- C · 409/422 falsos y límite de tasa ---------------------------------------------------------

class RacyGitHub(FakeGitHub):
    """GitHub que rechaza con 409 los primeros PUT aunque el archivo no cambió (commit simultáneo)."""

    def __init__(self, spurious=0, limited=None):
        super().__init__()
        self.spurious = spurious
        self.limited = list(limited or [])          # respuestas de límite de tasa para las próximas llamadas
        self.put_calls = 0

    def request(self, method, url, headers=None, json=None, timeout=None, **kw):
        if self.limited:
            return self.limited.pop(0)
        if method == "PUT":
            self.put_calls += 1
            if self.spurious > 0:
                self.spurious -= 1
                return Resp(409, text="is at abc but expected def")
        return super().request(method, url, headers=headers, json=json, timeout=timeout, **kw)


class LimitedResp(Resp):
    def __init__(self, status, headers, text="API rate limit exceeded"):
        super().__init__(status, text=text)
        self.headers = headers


@pytest.fixture
def racy(monkeypatch):
    def make(**kwargs):
        fake = RacyGitHub(**kwargs)
        monkeypatch.setattr(sm.requests, "request", fake.request)
        return fake

    sleeps = []
    monkeypatch.setattr(sm.time, "sleep", sleeps.append)
    secrets = {"GITHUB_TOKEN": "token-de-prueba", "GITHUB_REPO": "org/repo-de-prueba"}
    monkeypatch.setattr(sm, "safe_secret", lambda key, default=None: secrets.get(key, default))
    make.sleeps = sleeps
    return make


def test_a_spurious_409_is_retried_and_is_not_reported_as_a_conflict(racy):
    fake = racy()
    db = LabStorage()
    fake.spurious = 1                              # el siguiente PUT se rechaza aunque nada cambió
    _add(db, "LAB-001")
    assert fake.content == _local_bytes()
    status = sm.get_sync_status()
    assert status["ok"] is True and status["conflict"] is False
    assert fake.put_calls == 3                     # creación + rechazo + reintento


def test_a_persistent_409_with_an_unchanged_remote_stays_pending_without_a_conflict(racy):
    fake = racy()
    db = LabStorage()
    fake.spurious = 99
    _add(db, "LAB-001")
    status = sm.get_sync_status()
    assert status["ok"] is False and status["conflict"] is False         # no se pide decidir una versión
    assert "commit simultáneo" in status["message"]
    assert sm.get_sync_status()["pending"] is True
    fake.spurious = 0
    assert db.retry_sync() is True and fake.content == _local_bytes()      # y se publica cuando se despeja


def test_a_409_because_the_remote_really_changed_is_still_a_conflict(racy):
    fake = racy()
    db = LabStorage()
    _add(db, "LAB-001")
    real_put = fake.request

    def external_edit_then_reject(method, url, **kw):
        if method == "PUT" and not getattr(fake, "edited", False):
            fake.edited = True
            fake.set_external(b"EDITADO FUERA")         # alguien cambió el archivo entre el GET y el PUT
            return Resp(409, text="sha mismatch")
        return real_put(method, url, **kw)

    fake.request = external_edit_then_reject
    sm.requests.request = external_edit_then_reject
    _add(db, "LAB-002")
    assert sm.get_sync_status()["conflict"] is True
    assert fake.content == b"EDITADO FUERA"                            # nunca se pisa lo desconocido


def test_rate_limit_with_a_short_retry_after_is_waited_out(racy):
    limited = LimitedResp(403, {"Retry-After": "3", "X-RateLimit-Remaining": "0"})
    fake = racy(limited=[limited])
    db = LabStorage()
    _add(db, "LAB-001")
    assert fake.content == _local_bytes()
    assert sm.get_sync_status()["ok"] is True
    assert 3 in racy.sleeps or 3.0 in racy.sleeps                      # esperó lo que pidió GitHub


def test_rate_limit_with_a_long_wait_is_reported_as_such_not_as_an_invalid_token(racy):
    long_wait = {"Retry-After": "120", "X-RateLimit-Remaining": "0"}
    fake = racy()
    db = LabStorage()
    fake.limited = [LimitedResp(403, long_wait)] * 12
    _add(db, "LAB-001")
    status = sm.get_sync_status()
    assert status["ok"] is False and status["conflict"] is False
    assert "límite de tasa" in status["message"] and "GITHUB_TOKEN" not in status["message"]
    fake.limited = []
    assert db.retry_sync() is True and fake.content == _local_bytes()


def test_a_plain_403_is_still_a_permission_problem():
    assert "GITHUB_TOKEN" in sm._describe_github_error(403, "Resource not accessible", {})
    assert "GITHUB_TOKEN" in sm._describe_github_error(403, "Resource not accessible")
    assert "límite de tasa" in sm._describe_github_error(403, "x", {"retry-after": "5"})
    assert "límite de tasa" in sm._describe_github_error(429, "x")
    assert "límite de tasa" in sm._describe_github_error(403, "You have exceeded a secondary rate limit")


# --- C · las fotos no hacen commit a la vez que la base -------------------------------------------

def test_photo_uploads_and_deletes_hold_the_shared_commit_lock(monkeypatch):
    from core import photos

    held = []

    class Session:
        def request(self, method, url, **kw):
            held.append((method, sm._push_lock.locked()))
            return Resp(200 if method != "PUT" else 201, {"content": {"sha": "abc"}})

    monkeypatch.setattr(sm, "safe_secret", lambda key, default=None: "x")
    photos.upload("fotos/a.jpg", b"datos", "mensaje", session=Session())
    photos.remove_remote("fotos/a.jpg", "abc", "mensaje", session=Session())
    assert ("PUT", True) in held and ("DELETE", True) in held
    assert not sm._push_lock.locked()
