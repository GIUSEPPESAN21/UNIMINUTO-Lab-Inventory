# -*- coding: utf-8 -*-
"""Sincronizacion con GitHub: escritura atomica, push serializado y sin carreras,
y deteccion de cambios remotos externos (nunca se pisa una version desconocida).

Se usa un GitHub simulado que imita la semantica de la API de contenidos: GET
devuelve sha/contenido (o 404), PUT exige el sha vigente (409 si esta obsoleto)."""

import base64
import hashlib
import os
import threading
import time

import pytest
import requests

from core import storage as sm
from core.storage import LabStorage

ACTOR = "profesor@uniminuto.edu.co"
_REAL_SLEEP = time.sleep  # los tests parchean time.sleep globalmente; este es el original


class Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class FakeGitHub:
    """Un unico archivo en el repo simulado."""

    def __init__(self):
        self.content = None  # bytes o None (no existe)
        self.puts = []
        self.fail_next = []   # respuestas forzadas (status) para las proximas llamadas
        self._lock = threading.Lock()
        self.active_puts = 0
        self.max_concurrent_puts = 0

    @property
    def sha(self):
        return hashlib.sha1(self.content).hexdigest() if self.content is not None else None

    def set_external(self, content: bytes):
        """Simula que alguien edito el archivo fuera de la app."""
        self.content = content

    def request(self, method, url, headers=None, json=None, timeout=None, **_):
        if self.fail_next:
            return Resp(self.fail_next.pop(0), text="boom")
        if method == "GET":
            if self.content is None:
                return Resp(404)
            return Resp(200, {
                "sha": self.sha, "encoding": "base64",
                "content": base64.b64encode(self.content).decode(),
            })
        assert method == "PUT"
        with self._lock:
            self.active_puts += 1
            self.max_concurrent_puts = max(self.max_concurrent_puts, self.active_puts)
        _REAL_SLEEP(0.12)  # latencia de red real: sin serializar, los hilos se solapan aqui
        try:
            sent_sha = json.get("sha")
            if self.content is not None and sent_sha != self.sha:
                return Resp(409, text="sha mismatch")
            if self.content is None and sent_sha:
                return Resp(422, text="sha for missing file")
            created = self.content is None
            self.content = base64.b64decode(json["content"])
            self.puts.append(json.get("message"))
            return Resp(201 if created else 200, {"content": {"sha": self.sha}})
        finally:
            with self._lock:
                self.active_puts -= 1


@pytest.fixture
def github(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(sm.requests, "request", fake.request)
    monkeypatch.setattr(sm.time, "sleep", lambda _s: None)
    secrets = {"GITHUB_TOKEN": "token-de-prueba", "GITHUB_REPO": "org/repo-de-prueba"}
    monkeypatch.setattr(sm, "safe_secret", lambda key, default=None: secrets.get(key, default))
    return fake


def _add(db, code, **extra):
    db.save_item({"name": "Item", "item_type": "standalone", "quantity": 1, **extra},
                 code, is_new=True, actor_email=ACTOR)


def _local_bytes():
    with open(sm.EXCEL_PATH, "rb") as f:
        return f.read()


# --- publicacion basica -------------------------------------------------------

def test_first_write_creates_the_remote_file_synchronously(github):
    db = LabStorage()  # la creacion de la base vacia ya se publica
    assert github.content is not None
    _add(db, "LAB-001")
    assert github.content == _local_bytes()  # al volver de save_item, ya esta publicado
    status = sm.get_sync_status()
    assert status["ok"] is True and status["conflict"] is False


def test_startup_pull_adopts_remote_and_later_writes_publish_with_its_sha(github):
    seed = LabStorage()
    _add(seed, "LAB-001")
    remote = github.content
    github.puts.clear()
    os.remove(sm.EXCEL_PATH)          # contenedor nuevo: sin archivo local
    sm._cached_dfs = None
    sm._reset_sync_state()

    db = LabStorage()                  # descarga la base remota
    assert db.get_item("LAB-001") is not None
    assert _local_bytes() == remote
    _add(db, "LAB-002")
    assert len(github.puts) == 1 and github.content == _local_bytes()
    assert sm.get_sync_status()["ok"] is True


def test_no_dirty_write_means_no_extra_push(github):
    db = LabStorage()
    _add(db, "LAB-001")
    pushes = len(github.puts)
    db.get_item("LAB-001")
    db.get_all_items()
    assert len(github.puts) == pushes  # leer nunca publica


# --- conflictos: no pisar lo que la app no conoce ------------------------------

def test_external_remote_change_blocks_the_push_and_flags_conflict(github):
    db = LabStorage()
    _add(db, "LAB-001")
    github.set_external(b"EDITADO A MANO FUERA DE LA APP")

    _add(db, "LAB-002")                 # se guarda localmente...
    assert db.get_item("LAB-002") is not None
    assert github.content == b"EDITADO A MANO FUERA DE LA APP"   # ...pero NO pisa lo remoto
    status = sm.get_sync_status()
    assert status["ok"] is False and status["conflict"] is True
    assert "administrador" in status["message"].lower()


def test_resolve_conflict_keeping_local_overwrites_remote(github):
    db = LabStorage()
    _add(db, "LAB-001")
    github.set_external(b"OTRA VERSION")
    _add(db, "LAB-002")

    ok, _ = db.resolve_sync_conflict("local")
    assert ok
    assert github.content == _local_bytes()
    assert sm.get_sync_status()["conflict"] is False
    _add(db, "LAB-003")                 # y el flujo normal se reanuda
    assert github.content == _local_bytes()


def test_resolve_conflict_keeping_remote_discards_local_changes(github):
    db = LabStorage()
    _add(db, "LAB-001")
    remote_v1 = github.content          # GitHub con solo LAB-001
    _add(db, "LAB-002")                 # la app publica v2 y la recuerda
    github.set_external(remote_v1)      # alguien restaura v1 por fuera: sha distinto del recordado
    _add(db, "LAB-003")                 # cambio local que NO se publica (conflicto)
    assert sm.get_sync_status()["conflict"] is True

    ok, message = db.resolve_sync_conflict("remote")
    assert ok and "version de GitHub" in message
    assert _local_bytes() == remote_v1
    assert db.get_item("LAB-001") is not None
    assert db.get_item("LAB-002") is None and db.get_item("LAB-003") is None
    assert sm.get_sync_status()["conflict"] is False
    _add(db, "LAB-004")                 # el flujo normal se reanuda
    assert github.content == _local_bytes()


def test_never_overwrites_a_remote_it_could_not_download(github):
    """Si al arrancar GitHub falló y quedaba una copia local vieja, un push no debe
    pisar la base real: el SHA remoto desconocido se trata como conflicto."""
    seed = LabStorage()
    _add(seed, "LAB-REAL")
    real_remote = github.content
    sm._cached_dfs = None
    sm._reset_sync_state()                       # la app «olvida» qué versión remota conoce

    github.fail_next = [500, 500, 500]          # el pull inicial agota sus reintentos
    db = LabStorage()                            # sigue con la copia local
    _add(db, "LAB-NUEVO")

    assert github.content == real_remote         # la base real sigue intacta
    assert sm.get_sync_status()["conflict"] is True


def test_startup_without_local_copy_and_without_github_stops_instead_of_inventing_an_empty_base(github):
    """Sin copia local y con GitHub caído (no es un 404) la app no arranca con datos vacíos."""
    seed = LabStorage()
    _add(seed, "LAB-REAL")
    real_remote = github.content
    os.remove(sm.EXCEL_PATH)
    sm._cached_dfs = None
    sm._reset_sync_state()

    github.fail_next = [500, 500, 500]
    with pytest.raises(sm.RemoteUnavailableError) as raised:
        LabStorage()
    assert "no hay una copia local" in str(raised.value)
    assert not os.path.exists(sm.EXCEL_PATH)             # no se creó un libro vacío
    assert github.content == real_remote                 # y la base real quedó intacta

    github.fail_next = []                                 # al volver GitHub, el reintento arranca bien
    assert LabStorage().get_item("LAB-REAL") is not None


def test_a_missing_remote_file_is_not_an_outage_and_a_new_base_is_created(github):
    assert github.content is None                        # 404: la base aún no existe en GitHub
    LabStorage()
    assert github.content is not None


# --- fallos transitorios y reintentos -----------------------------------------

def test_transient_server_errors_are_retried(github):
    db = LabStorage()
    github.fail_next = [503, 502]                # GET falla dos veces, la 3.ª funciona
    _add(db, "LAB-001")
    assert github.content == _local_bytes()
    assert sm.get_sync_status()["ok"] is True


def test_failed_push_keeps_changes_pending_and_retry_publishes_them(github):
    db = LabStorage()
    github.fail_next = [500] * 9                 # agota los reintentos del GET y PUT
    _add(db, "LAB-001")
    assert sm.get_sync_status()["ok"] is False
    assert github.content != _local_bytes()

    github.fail_next = []
    assert db.retry_sync() is True
    assert github.content == _local_bytes()


def test_permission_error_is_reported_with_actionable_message(github):
    db = LabStorage()
    github.fail_next = [403]
    _add(db, "LAB-001")
    status = sm.get_sync_status()
    assert status["ok"] is False and "GITHUB_TOKEN" in status["message"]


# --- concurrencia y atomicidad -------------------------------------------------

def test_concurrent_writes_never_overlap_and_remote_ends_with_latest(github):
    db = LabStorage()
    errors = []

    def worker(n):
        try:
            _add(db, f"LAB-{n:03d}")
        except Exception as exc:  # pragma: no cover - solo si falla la prueba
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(1, 9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert github.max_concurrent_puts == 1                  # push serializado
    assert github.content == _local_bytes()                 # gana la ULTIMA version
    assert len(db.get_all_items()) == 8                     # ninguna escritura se perdio


def test_failed_write_leaves_previous_excel_intact(github, monkeypatch):
    db = LabStorage()
    _add(db, "LAB-001")
    before = _local_bytes()

    def boom(src, dst):
        raise OSError("disco lleno")

    with monkeypatch.context() as scoped:
        scoped.setattr(sm.os, "replace", boom)
        with pytest.raises(OSError):
            _add(db, "LAB-002")

    assert _local_bytes() == before                         # nunca un archivo a medias
    assert not os.path.exists(f"{sm.EXCEL_PATH}.tmp")       # sin basura temporal


def test_describe_and_request_helper_surface_final_network_error(monkeypatch):
    monkeypatch.setattr(sm.time, "sleep", lambda _s: None)

    def always_down(*_a, **_k):
        raise requests.ConnectionError("sin red")

    monkeypatch.setattr(sm.requests, "request", always_down)
    with pytest.raises(requests.ConnectionError):
        sm._github_request("GET", "https://api.github.com/x", attempts=3)
