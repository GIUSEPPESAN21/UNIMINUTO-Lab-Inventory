# -*- coding: utf-8 -*-
"""PDF con las etiquetas de todos los productos de un contenedor (Inventario)."""

import pytest

from core import labels
from core.storage import LabStorage

ACTOR = "profe@uniminuto.edu.co"
MASTER = "2-1-01-00-000"


def _kid(code, name):
    return {"id": code, "name": name, "item_type": "child", "parent_id": MASTER, "category": "Piezas"}


@pytest.fixture
def container():
    storage = LabStorage()
    storage.save_item({"name": "Caja de pines", "item_type": "master", "location": "E2"}, MASTER,
                      is_new=True, actor_email=ACTOR)
    for code, name in (("2-1-01-10-000", "Pin 10"), ("2-1-01-02-000", "Pin 2"), ("2-1-01-01-000", "Pin 1")):
        storage.save_item({"name": name, "item_type": "child", "parent_id": MASTER, "quantity": 3}, code,
                          is_new=True, actor_email=ACTOR)
    return storage


def test_pdf_has_one_page_per_child_in_natural_code_order(container, monkeypatch):
    import pypdfium2

    seen = []
    original = labels.generate_labels_pdf_bytes

    def spy(items, spec=None, title="Etiquetas"):
        seen.append(([item["id"] for item in items], title))
        return original(items, spec, title=title)

    monkeypatch.setattr(labels, "generate_labels_pdf_bytes", spy)
    master = container.get_item(MASTER)
    pdf = labels.generate_container_labels_pdf_bytes(master, container.get_children(MASTER))
    assert seen == [(["2-1-01-01-000", "2-1-01-02-000", "2-1-01-10-000"], "Etiquetas de Caja de pines")]
    assert len(pypdfium2.PdfDocument(pdf)) == 3


def test_unprintable_codes_are_skipped_and_empty_container_gives_none(container):
    kids = container.get_children(MASTER) + [_kid("CODIGO MALO ñ", "Raro")]
    assert len(labels.printable_items(kids)) == 3
    assert labels.generate_container_labels_pdf_bytes({"name": "Vacía"}, []) is None
    assert labels.generate_container_labels_pdf_bytes({"name": "Vacía"}, [_kid("CODIGO MALO ñ", "Raro")]) is None


def test_natural_order_of_codes():
    assert sorted(["LAB-10", "LAB-2", "LAB-1"], key=labels._natural_key) == ["LAB-1", "LAB-2", "LAB-10"]


# --- vista real ---------------------------------------------------------------------------

def _script():
    import streamlit as st

    from core.storage import LabStorage
    from views import inventario

    st.session_state.storage = LabStorage()
    st.session_state.user = {"id": "p1", "role": "profesor", "full_name": "Prof Uno",
                             "institutional_email": "profe@uniminuto.edu.co"}
    inventario.render()


def test_inventory_offers_the_download_only_for_containers_with_products(container):
    pytest.importorskip("streamlit.testing.v1")
    from streamlit.testing.v1 import AppTest

    container.save_item({"name": "Caja vacía", "item_type": "master"}, "2-1-02-00-000", is_new=True,
                        actor_email=ACTOR)
    at = AppTest.from_function(_script, default_timeout=60)
    at.run()
    assert not at.exception
    labels_shown = [element.proto.label for element in at.get("download_button")]
    assert any(label.endswith("Etiquetas de los 3 productos") for label in labels_shown)
    assert len([label for label in labels_shown if "productos" in label]) == 1    # la caja vacía no la ofrece
