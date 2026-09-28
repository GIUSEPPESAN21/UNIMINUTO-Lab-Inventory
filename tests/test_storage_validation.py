# -*- coding: utf-8 -*-
"""Pruebas de core/storage: los items NUEVOS deben cumplir uno de los formatos
de codigo de core/barcode.py (GLIOPS V3 o codigos libres numericos y
alfanumericos); los items ya existentes (edicion) no se revalidan, para no
romper catalogos cargados con la nomenclatura anterior. Incluye un guardado y
una recarga reales en un Excel temporal para comprobar que el codigo se
conserva exacto (ceros a la izquierda y letras)."""

import pandas as pd
import pytest

from core import storage as storage_module
from core.storage import SHEET_COLUMNS, _validate_item_data

EMPTY_ITEMS = pd.DataFrame(columns=SHEET_COLUMNS["items"])
STANDALONE = {"item_type": "standalone", "parent_id": ""}


def test_new_item_with_valid_standard_code_is_accepted():
    _validate_item_data(STANDALONE, EMPTY_ITEMS, "1-2-05-12-001", is_new=True)


def test_new_item_with_valid_mesa_code_is_accepted():
    _validate_item_data(STANDALONE, EMPTY_ITEMS, "M1-E2", is_new=True)


@pytest.mark.parametrize("code", ["0012345", "LAB-MIC-01", "CAJA-001"])
def test_new_item_with_free_code_is_accepted(code):
    _validate_item_data(STANDALONE, EMPTY_ITEMS, code, is_new=True)


@pytest.mark.parametrize("code", ["LAB MIC 01", "LAB-MÍC-01", "M3-E1"])
def test_new_item_with_invalid_code_is_rejected(code):
    with pytest.raises(ValueError):
        _validate_item_data(STANDALONE, EMPTY_ITEMS, code, is_new=True)


def test_editing_existing_item_does_not_revalidate_legacy_code():
    """Un item cargado antes de la validacion (id fuera de todo formato)
    debe poder seguir editandose sin que su id sea rechazado."""
    _validate_item_data(STANDALONE, EMPTY_ITEMS, "CAJA 001", is_new=False)


@pytest.fixture
def excel_storage(tmp_path, monkeypatch):
    """LabStorage real sobre un Excel temporal y sin GitHub: nunca toca la base
    del laboratorio, aunque el equipo tenga Secrets reales configurados."""
    monkeypatch.setattr(storage_module, "EXCEL_PATH", str(tmp_path / "lab_db_test.xlsx"))
    monkeypatch.setattr(storage_module, "_cached_dfs", None)
    monkeypatch.setattr(storage_module, "_is_github_configured", lambda: False)
    return storage_module.LabStorage()


@pytest.mark.parametrize("code", ["0012345", "LAB-MIC-01"])
def test_free_codes_are_saved_and_reloaded_exactly_from_excel(excel_storage, code):
    excel_storage.save_item(
        {"name": "Microscopio", "item_type": "standalone", "quantity": 2},
        code, is_new=True, actor_email="profesor@uniminuto.edu.co",
    )
    storage_module._cached_dfs = None  # obliga a releer el .xlsx desde disco

    item = excel_storage.get_item(code)
    assert item is not None
    assert item["id"] == code
    assert item["name"] == "Microscopio"
    assert [i["id"] for i in excel_storage.get_all_items()] == [code]


def test_numeric_code_keeps_leading_zeros_after_reload(excel_storage):
    excel_storage.save_item(
        {"name": "Osciloscopio", "item_type": "standalone", "quantity": 1},
        "0012345", is_new=True, actor_email="profesor@uniminuto.edu.co",
    )
    storage_module._cached_dfs = None

    assert excel_storage.get_item("12345") is None
    assert excel_storage.get_item("0012345")["id"] == "0012345"
