# -*- coding: utf-8 -*-
"""Pestaña 🖨️ Etiquetas de Inventario (AppTest con la app real): vista previa,
guardado del tamaño, tamaño personalizado y contenido opcional."""

import re

import pytest

pytest.importorskip("streamlit.testing.v1")

from streamlit.testing.v1 import AppTest  # noqa: E402

from core import labels  # noqa: E402
from core.storage import LabStorage  # noqa: E402


def _inventory_script():
    import streamlit as st

    from core.storage import LabStorage
    from views import inventario

    st.session_state.storage = LabStorage()
    st.session_state.user = {
        "id": "p1", "role": "profesor", "full_name": "Prof Uno",
        "institutional_email": "prof@uniminuto.edu.co",
    }
    inventario.render()


def _open():
    at = AppTest.from_function(_inventory_script)
    at.run()
    assert not at.exception
    return at


def test_label_tab_shows_saved_size_preview_and_what_was_omitted():
    at = _open()
    assert any("Configuración guardada: **50 × 25 mm · 203 dpi**" in c.value for c in at.caption)
    assert at.selectbox(key="label_cfg_preset").value == "50x25"
    assert {m.label for m in at.metric} >= {"Texto más pequeño", "Alto de las barras"}
    assert any("no caben a un tamaño legible" in i.value for i in at.info)
    assert at.button(key="label_cfg_save").disabled  # nada que guardar todavia


def test_saving_a_new_size_is_persisted_for_every_label():
    at = _open()
    at.selectbox(key="label_cfg_preset").select("100x50").run()
    assert not at.button(key="label_cfg_save").disabled
    at.button(key="label_cfg_save").click().run()
    assert not at.exception

    spec = labels.load_label_spec(LabStorage())
    assert (spec.width_mm, spec.height_mm, spec.dpi) == (100, 50, 203)
    pdf, _ = labels.item_label_files({"id": "LAB-001", "name": "Multímetro"}, spec)
    media_box = re.search(rb"/MediaBox \[0 0 ([0-9.]+) ([0-9.]+)\]", pdf)
    assert float(media_box.group(1)) * 25.4 / 72 == pytest.approx(100, abs=0.01)
    assert any("100 × 50 mm · 203 dpi" in c.value for c in _open().caption)


def test_custom_size_resolution_and_content_are_saved():
    at = _open()
    at.selectbox(key="label_cfg_preset").select("custom").run()
    at.number_input(key="label_cfg_width").set_value(75.0)
    at.number_input(key="label_cfg_height").set_value(35.0)
    at.radio(key="label_cfg_dpi").set_value(300)
    at.checkbox(key="label_cfg_category").uncheck()
    at.run()
    assert not at.exception
    at.button(key="label_cfg_save").click().run()

    spec = labels.load_label_spec(LabStorage())
    assert (spec.width_mm, spec.height_mm, spec.dpi) == (75, 35, 300)
    assert "category" not in spec.content and "brand" in spec.content


def _scan_script():
    import streamlit as st

    from core import barcode, labels
    from core.storage import LabStorage
    from views import escanear

    storage = LabStorage()
    if not storage.get_item("LAB-001"):
        storage.save_item(
            {"name": "Multímetro", "item_type": "standalone", "quantity": 2},
            "LAB-001", is_new=True, actor_email="prof@uniminuto.edu.co",
        )
        labels.save_label_spec(storage, labels.LabelSpec(100, 50))
    st.session_state.storage = storage
    st.session_state.user = {
        "id": "e1", "role": "estudiante", "full_name": "Ana Prueba",
        "institutional_email": "ana@uniminuto.edu.co",
    }
    st.session_state.scan_result = barcode.scan(storage, "LAB-001")
    escanear.render()


def test_scan_page_offers_labels_in_the_configured_size():
    at = AppTest.from_function(_scan_script)
    at.run()
    assert not at.exception
    downloads = at.get("download_button")
    assert len(downloads) == 2
    assert downloads[0].proto.label == "🏷️ PDF 100 × 50 mm"
    assert any("100 × 50 mm · 203 dpi" in c.value for c in at.caption)
