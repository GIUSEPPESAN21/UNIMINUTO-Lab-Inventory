# -*- coding: utf-8 -*-
"""_atomic_write_bytes: reintento ante PermissionError (Windows: handle que
todavia no se solto sobre el destino), nombre temporal unico por escritura, y
limpieza del temporal si la escritura termina fallando."""

import glob
import os
import threading
import time

import pytest

from core import storage as sm


def test_write_succeeds_on_first_try_without_retry_delay(tmp_path, monkeypatch):
    path = str(tmp_path / "libro.xlsx")
    sleeps = []
    monkeypatch.setattr(sm.time, "sleep", lambda s: sleeps.append(s))

    sm._atomic_write_bytes(path, b"contenido-1")

    assert open(path, "rb").read() == b"contenido-1"
    assert sleeps == []                        # camino feliz: sin reintentos, sin espera
    assert not glob.glob(f"{path}.*.tmp")       # sin basura temporal


def test_retries_and_recovers_from_a_transient_permission_error(tmp_path, monkeypatch):
    path = str(tmp_path / "libro.xlsx")
    real_replace = sm.os.replace
    attempts = {"n": 0}

    def flaky_replace(src, dst):
        attempts["n"] += 1
        if attempts["n"] < 3:                  # las 2 primeras veces el archivo "sigue abierto"
            raise PermissionError("[WinError 5] Acceso denegado")
        return real_replace(src, dst)

    sleeps = []
    monkeypatch.setattr(sm.os, "replace", flaky_replace)
    monkeypatch.setattr(sm.time, "sleep", lambda s: sleeps.append(s))

    sm._atomic_write_bytes(path, b"contenido-2")

    assert attempts["n"] == 3
    assert open(path, "rb").read() == b"contenido-2"
    assert sleeps == [
        sm._ATOMIC_REPLACE_BASE_DELAY_SECONDS,
        sm._ATOMIC_REPLACE_BASE_DELAY_SECONDS * 2,
    ]                                            # backoff creciente
    assert not glob.glob(f"{path}.*.tmp")


def test_gives_up_with_a_clear_error_after_max_attempts(tmp_path, monkeypatch):
    path = str(tmp_path / "libro.xlsx")

    def always_locked(src, dst):
        raise PermissionError("[WinError 5] Acceso denegado")

    monkeypatch.setattr(sm.os, "replace", always_locked)
    monkeypatch.setattr(sm.time, "sleep", lambda _s: None)

    with pytest.raises(PermissionError) as exc_info:
        sm._atomic_write_bytes(path, b"contenido-3")

    assert str(sm._ATOMIC_REPLACE_MAX_ATTEMPTS) in str(exc_info.value)
    assert not os.path.exists(path)             # nunca se creo/sobrescribio el destino
    assert not glob.glob(f"{path}.*.tmp")        # el temporal se limpia, no queda basura


def test_two_concurrent_writes_never_collide_on_the_temp_name(tmp_path):
    path = str(tmp_path / "libro.xlsx")
    errors = []

    def write(payload):
        try:
            sm._atomic_write_bytes(path, payload)
        except Exception as exc:                # pragma: no cover - solo si falla la prueba
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(f"contenido-{n}".encode(),)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert os.path.exists(path)                 # alguna de las 8 escrituras gano, ninguna quedo a medias
    assert not glob.glob(f"{path}.*.tmp")        # ningun temporal quedo sin limpiar


@pytest.mark.skipif(os.name != "nt", reason="reproduce el bloqueo real de archivos de Windows")
def test_reproduces_the_real_windows_file_lock_and_recovers(tmp_path):
    """Reproduccion real (no simulada) del bug original: un lector que todavia no
    cerro su handle sobre el archivo bloquea el primer os.replace en Windows; el
    reintento con espera debe recuperarse solo en cuanto el handle se libera."""
    path = str(tmp_path / "libro.xlsx")
    sm._atomic_write_bytes(path, b"version-inicial")

    handle = open(path, "rb")           # handle real abierto sobre el archivo destino
    handle.read()

    def close_soon():
        time.sleep(0.12)
        handle.close()

    closer = threading.Thread(target=close_soon)
    closer.start()
    try:
        sm._atomic_write_bytes(path, b"version-nueva")   # el primer intento debe chocar de verdad
    finally:
        closer.join()

    assert open(path, "rb").read() == b"version-nueva"


def test_load_cache_closes_its_handle_so_a_write_right_after_does_not_block(tmp_path, monkeypatch):
    """Caso de la causa raiz original: _load_cache() leia el libro con
    pd.ExcelFile sin cerrar el handle; una escritura inmediatamente despues
    (mismo patron que un pull de GitHub tras cargar la cache) debe funcionar
    sin reintentos, porque el ``with`` ya libero el archivo."""
    import openpyxl

    path = str(tmp_path / "isolated_db.xlsx")
    openpyxl.Workbook().save(path)       # libro .xlsx real y valido: toma el camino de pd.ExcelFile

    monkeypatch.setattr(sm, "EXCEL_PATH", path)
    monkeypatch.setattr(sm, "_cached_dfs", None)

    sm._load_cache()                     # lee el libro real (no el atajo de FileNotFoundError)

    sleeps = []
    monkeypatch.setattr(sm.time, "sleep", lambda s: sleeps.append(s))
    sm._atomic_write_bytes(path, b"nuevo-contenido-tras-leer")

    assert sleeps == []                  # el with cerro el handle: la escritura no necesito reintentos
    assert open(path, "rb").read() == b"nuevo-contenido-tras-leer"
