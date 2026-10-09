# -*- coding: utf-8 -*-
"""Inventario (views/inventario.py) con la app real: catalogo agrupado por
contenedor y flujo «✨ Generar productos desde la descripción» de punta a punta
sobre los 3 contenedores reales del laboratorio."""

import pytest

pytest.importorskip("streamlit.testing.v1")

from streamlit.testing.v1 import AppTest  # noqa: E402

from core import inventory_suggestions as sug  # noqa: E402
from core import storage as storage_module  # noqa: E402
from core.storage import LabStorage  # noqa: E402
from test_inventory_suggestions import ACTOR, C1, C2, C3, EXPECTED, REAL, default_payloads  # noqa: E402


def _script():
    import streamlit as st

    from core.storage import LabStorage
    from views import inventario

    st.session_state.storage = LabStorage()
    st.session_state.user = {
        "id": "p1", "role": "profesor", "full_name": "Prof Uno",
        "institutional_email": "prof@uniminuto.edu.co",
    }
    inventario.render()


@pytest.fixture
def db():
    storage = LabStorage()
    for container in REAL:
        data = {k: container[k] for k in ("name", "category", "item_type", "location", "description")}
        storage.save_item(data, container["id"], is_new=True, actor_email=ACTOR)
    return storage


@pytest.fixture
def writes(monkeypatch):
    calls = []
    original = storage_module._write_and_sync

    def counting(dfs):
        calls.append(len(dfs["items"]))
        original(dfs)

    monkeypatch.setattr(storage_module, "_write_and_sync", counting)
    return calls


def _run():
    at = AppTest.from_function(_script, default_timeout=60)
    at.run()
    assert not at.exception
    return at


def _button(at, key):
    return at.button(key=key)


def _open_generator(at, container):
    _button(at, f"inv_gen_toggle_{container['id']}").click().run()
    assert not at.exception
    return at


def _text(elements) -> str:
    return " ".join(str(e.value) for e in elements)


def _editor(at):
    frames = [d.value for d in at.dataframe if list(d.value.columns) == list(sug.EDITOR_COLUMNS)]
    assert len(frames) == 1
    return frames[0]


def test_catalog_groups_by_container_and_offers_the_generator(db):
    at = _run()
    assert "3 item(s) encontrados." in _text(at.caption)
    toggles = [b for b in at.button if (b.key or "").startswith("inv_gen_toggle_")]
    # Ordenados por codigo (no por nombre: "Contendor 2" quedaria antes que "Contenedor 1").
    assert [b.key for b in toggles] == [f"inv_gen_toggle_{c['id']}" for c in REAL]
    assert all(b.label == ":material/auto_awesome: Generar productos desde la descripción" for b in toggles)
    assert "3 contenedor(es) describen productos que aún no están registrados" in _text(at.info)
    html = _text(at.markdown)
    assert "lab-stat" in html and "Pendientes de conteo" in html
    assert "5 sugerido(s) desde la descripción" in html and "13 sugerido(s)" in html
    assert "Contiene Piezas Lego de Pines 4x2 - 2x2 - 2x1" in html          # la descripcion en la tarjeta


def test_generator_shows_the_editable_preview_with_notes(db):
    at = _open_generator(_run(), C1)
    frame = _editor(at)
    assert list(zip(frame["Código"], frame["Nombre"], frame["Crear"])) == [
        (code, name, selected) for code, name, _feature, selected in EXPECTED[C1["id"]]
    ]
    assert frame["Cantidad"].tolist() == [0] * 5 and set(frame["Unidad"]) == {"unidad"}
    assert any(e.label == ":material/warning: Puntos para confirmar (3)" for e in at.expander)
    assert "«fichas» es un término genérico" in _text(at.markdown)
    create = _button(at, f"inv_gen_create_{C1['id']}")
    assert create.label == ":material/check_circle: Crear 5 producto(s)" and not create.disabled
    assert "Crear en una sola sincronización" in _text(at.markdown)            # timeline del flujo
    assert _button(at, f"inv_gen_toggle_{C1['id']}").label == ":material/close: Cerrar el generador"


def test_creating_from_the_preview_writes_once_and_closes(db, writes):
    at = _open_generator(_run(), C1)
    _button(at, f"inv_gen_create_{C1['id']}").click().run()
    assert not at.exception

    assert len(writes) == 1                                       # un solo commit para los 5 productos
    children = sorted(db.get_children(C1["id"]), key=lambda c: c["id"])
    assert [(c["id"], c["name"]) for c in children] == [(code, name) for code, name, *_ in EXPECTED[C1["id"]]]
    assert all(sug.is_pending_count(c) and c["created_by"] == "prof@uniminuto.edu.co" for c in children)
    assert "Se crearon 5 producto(s) en «Contenedor 1»; 5 quedan pendientes de conteo." in _text(at.success)
    assert at.session_state["inv_generate_for"] is None
    assert "8 item(s) encontrados." in _text(at.caption)
    assert "Cantidad por contar · unidad" in _text(at.caption)
    assert "5 por contar" in _text(at.markdown)

    # Al volver a abrirlo ya no hay nada nuevo que crear: no duplica.
    _open_generator(at, C1)
    assert "No hay productos nuevos por crear" in _text(at.markdown)
    assert "Ya registrados en este contenedor (no se duplican)" in _text(at.caption)


def test_container_2_creates_12_and_leaves_the_1x0_for_review(db, writes):
    at = _open_generator(_run(), C2)
    frame = _editor(at)
    assert frame["Crear"].tolist() == [True] * 12 + [False]
    assert "probablemente es «1x1»" in frame["Notas"].iloc[-1]
    assert _button(at, f"inv_gen_create_{C2['id']}").label == ":material/check_circle: Crear 12 producto(s)"
    _button(at, f"inv_gen_create_{C2['id']}").click().run()
    assert not at.exception and len(writes) == 1
    created = {c["name"] for c in db.get_children(C2["id"])}
    assert len(created) == 12 and "Pieza Lego con bisel 1x0" not in created
    assert "1 sugerido(s) desde la descripción" in _text(at.markdown)


def test_cancel_closes_the_generator_without_writing(db, writes):
    at = _open_generator(_run(), C3)
    _button(at, f"inv_gen_cancel_{C3['id']}").click().run()
    assert not at.exception and writes == []
    assert at.session_state["inv_generate_for"] is None
    assert not [d for d in at.dataframe if list(d.value.columns) == list(sug.EDITOR_COLUMNS)]


def test_filters_and_list_view(db):
    db.save_items_bulk(default_payloads(C3, db), actor_email=ACTOR)
    db.save_item({"name": "Multimetro", "item_type": "standalone", "quantity": 2, "min_stock_alert": 2},
                 "LAB-001", is_new=True, actor_email=ACTOR)
    at = _run()
    assert "7 item(s) encontrados." in _text(at.caption)
    assert "Ítems sin contenedor" in _text(at.markdown)

    at.selectbox(key="inv_stock").select("Pendiente de conteo").run()
    assert "3 item(s) encontrados." in _text(at.caption)
    at.selectbox(key="inv_stock").select("Stock bajo").run()
    assert "1 item(s) encontrados." in _text(at.caption) and "Multimetro" in _text(at.markdown)
    at.selectbox(key="inv_stock").select("Todos").run()

    at.text_input(key="inv_search").input("Contendor 3").run()       # el contenedor trae todos sus productos
    assert "4 item(s) encontrados." in _text(at.caption)
    at.text_input(key="inv_search").input("3x2").run()               # solo el producto que coincide
    assert "1 item(s) encontrados." in _text(at.caption)
    assert "Mostrando 1 de 3 producto(s)" in _text(at.caption)
    at.text_input(key="inv_search").input("").run()

    at.selectbox(key="inv_location").select(C3["location"]).run()   # incluye lo que esta dentro
    assert "4 item(s) encontrados." in _text(at.caption)
    at.selectbox(key="inv_location").select("Todos").run()

    at.radio(key="inv_view").set_value(":material/view_list: Lista").run()
    assert not at.exception
    assert "7 item(s) encontrados." in _text(at.caption)


def test_pending_product_editor_explains_what_to_do(db):
    db.save_items_bulk([sug.build_payload(
        {"code": "2-1-03-01-000", "name": "Pieza Lego con pines 4x2", "quantity": 0, "source": "x"}, C3,
    )], actor_email=ACTOR)
    at = AppTest.from_function(_script, default_timeout=60)
    at.session_state["editing_item_id"] = "2-1-03-01-000"
    at.run()
    assert not at.exception
    assert "pendiente de conteo" in _text(at.info)


def test_container_without_description_has_no_generator_and_html_is_escaped(db):
    db.save_item({"name": "<b>Caja</b>", "item_type": "master", "description": "<script>alert(1)</script>"},
                 "2-1-04-00-000", is_new=True, actor_email=ACTOR)
    db.save_item({"name": "Vacio", "item_type": "master"}, "2-1-05-00-000", is_new=True, actor_email=ACTOR)
    at = _run()
    keys = {b.key for b in at.button}
    assert "inv_gen_toggle_2-1-05-00-000" not in keys and "inv_gen_toggle_2-1-04-00-000" in keys
    html = _text(at.markdown)
    assert "&lt;script&gt;" in html and "<script>alert" not in html and "&lt;b&gt;Caja" in html
