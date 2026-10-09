# -*- coding: utf-8 -*-
"""Productos propuestos desde la descripcion de un contenedor
(core/inventory_suggestions.py) y alta masiva en una sola escritura
(LabStorage.save_items_bulk)."""

import math

import pytest

from core import inventory_suggestions as sug
from core import storage as storage_module
from core.storage import LabStorage

ACTOR = "prof@uniminuto.edu.co"

# Los 3 contenedores reales del laboratorio, tal como estan registrados
# (incluidos los errores de digitacion "Contendor").
C1 = {
    "id": "2-1-01-00-000", "name": "Contenedor 1", "category": "Piezas Lego", "item_type": "master",
    "location": "Estantería- 2; Piso-1; Contenedor-1.", "status": "active", "quantity": 0,
    "description": "Contiene Piezas Lego de Pines 4x2 - 2x2 - 2x1 \nAdemás, Contiene un Caja con "
                   "Separadores de Fichas y Pieza lego Lisas de 2x2",
}
C2 = {
    "id": "2-1-02-00-000", "name": "Contendor 2", "category": "Piezas Lego", "item_type": "master",
    "location": "Estantería- 2; Piso-1; Contenedor-2.", "status": "active", "quantity": 0,
    "description": "Contiene Piezas Lego de Pines 6x2 - 6x1 - 4x1 - Bases de 1 pin \nAdemás, Contiene "
                   "una Caja con Puertas y Ventanas, Piezas lego Lisas de 3x2 - 4x2 - 6x2, Hay piezas con "
                   "Biseles 3x2 - 2x2 - 4x1 - 2x1 - 1x0",
}
C3 = {
    "id": "2-1-03-00-000", "name": "Contendor 3", "category": "Piezas Lego", "item_type": "master",
    "location": "Estantería- 2; Piso-1; Contenedor-3.", "status": "active", "quantity": 0,
    "description": "Contiene Piezas Lego de Pines 4x2 - 3x2 - 2x1.",
}
REAL = [C1, C2, C3]

# (codigo, nombre, caracteristica, crear por defecto)
EXPECTED = {
    "2-1-01-00-000": [
        ("2-1-01-01-000", "Pieza Lego con pines 4x2", "Con pines", True),
        ("2-1-01-02-000", "Pieza Lego con pines 2x2", "Con pines", True),
        ("2-1-01-03-000", "Pieza Lego con pines 2x1", "Con pines", True),
        ("2-1-01-04-000", "Caja con separadores de fichas", "Caja", True),
        ("2-1-01-05-000", "Pieza Lego lisa 2x2", "Lisa", True),
    ],
    "2-1-02-00-000": [
        ("2-1-02-01-000", "Pieza Lego con pines 6x2", "Con pines", True),
        ("2-1-02-02-000", "Pieza Lego con pines 6x1", "Con pines", True),
        ("2-1-02-03-000", "Pieza Lego con pines 4x1", "Con pines", True),
        ("2-1-02-04-000", "Base Lego de 1 pin", "Base", True),
        ("2-1-02-05-000", "Caja con puertas y ventanas", "Caja", True),
        ("2-1-02-06-000", "Pieza Lego lisa 3x2", "Lisa", True),
        ("2-1-02-07-000", "Pieza Lego lisa 4x2", "Lisa", True),
        ("2-1-02-08-000", "Pieza Lego lisa 6x2", "Lisa", True),
        ("2-1-02-09-000", "Pieza Lego con bisel 3x2", "Con bisel", True),
        ("2-1-02-10-000", "Pieza Lego con bisel 2x2", "Con bisel", True),
        ("2-1-02-11-000", "Pieza Lego con bisel 4x1", "Con bisel", True),
        ("2-1-02-12-000", "Pieza Lego con bisel 2x1", "Con bisel", True),
        ("2-1-02-13-000", "Pieza Lego con bisel 1x0", "Con bisel", False),
    ],
    "2-1-03-00-000": [
        ("2-1-03-01-000", "Pieza Lego con pines 4x2", "Con pines", True),
        ("2-1-03-02-000", "Pieza Lego con pines 3x2", "Con pines", True),
        ("2-1-03-03-000", "Pieza Lego con pines 2x1", "Con pines", True),
    ],
}


def _notes(proposal) -> str:
    return " ".join(proposal["notes"])


# ---------------------------------------------------------------------------
# Los 3 contenedores reales
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("container", REAL, ids=lambda c: c["id"])
def test_real_containers_produce_the_expected_products(container):
    result = sug.suggest_products(container, REAL)
    got = [(p["code"], p["name"], p["feature"], p["selected"]) for p in result["proposals"]]
    assert got == EXPECTED[container["id"]]
    assert result["existing"] == [] and result["warnings"] == [] and result["unparsed"] == []
    assert result["container_name"] == container["name"]

    base = container["location"].rstrip(".")
    for proposal in result["proposals"]:
        box = proposal["code"].split("-")[3]
        assert proposal["item_type"] == "child"
        assert proposal["parent_id"] == container["id"]
        assert proposal["category"] == "Piezas Lego"
        assert proposal["unit"] == "unidad"
        assert proposal["quantity"] == 0 and proposal["pending_count"] is True
        assert proposal["min_stock_alert"] == 0
        assert proposal["location"] == f"{base}; Caja {box}."


def test_container_1_notes_flag_the_vague_box_and_the_y_coordination():
    proposals = {p["name"]: p for p in sug.suggest_products(C1, REAL)["proposals"]}
    box = proposals["Caja con separadores de fichas"]
    assert "«fichas» es un término genérico" in _notes(box)
    assert "Caja física" in _notes(box)
    smooth = proposals["Pieza Lego lisa 2x2"]
    assert "Venía unido con «y» a «Caja con separadores de fichas»" in _notes(smooth)
    assert all(not p["notes"] for name, p in proposals.items() if "pines" in name)
    assert smooth["source"] == "Pieza lego Lisas de 2x2"
    assert proposals["Pieza Lego con pines 2x2"]["source"] == "Piezas Lego de Pines … 2x2"


def test_container_2_flags_the_impossible_1x0_measure_instead_of_inventing_1x1():
    proposals = {p["name"]: p for p in sug.suggest_products(C2, REAL)["proposals"]}
    odd = proposals["Pieza Lego con bisel 1x0"]
    assert odd["selected"] is False
    assert "«1x0» tiene una dimensión 0" in _notes(odd) and "probablemente es «1x1»" in _notes(odd)
    assert "Pieza Lego con bisel 1x1" not in proposals
    # "Caja con Puertas y Ventanas" es UNA caja: la "y" no la parte en dos productos.
    assert "Caja con puertas y ventanas" in proposals and not any(n.startswith("Ventana") for n in proposals)
    assert proposals["Base Lego de 1 pin"]["source"] == "Bases de 1 pin"


def test_container_3_with_trailing_period():
    names = [p["name"] for p in sug.suggest_products(C3, [C3])["proposals"]]
    assert names == ["Pieza Lego con pines 4x2", "Pieza Lego con pines 3x2", "Pieza Lego con pines 2x1"]


# ---------------------------------------------------------------------------
# Interpretacion del texto (cualquier contenedor futuro)
# ---------------------------------------------------------------------------

def _names(text, context="Piezas Lego"):
    return [e["name"] for e in sug.parse_description(text, context=context)["entries"]]


@pytest.mark.parametrize("text", [
    "Piezas Lego de pines 4x2, 2x2 y 2x1",
    "piezas lego de pines 4 x 2 - 2 X 2 - 2×1",
    "PIEZAS LEGO DE PINES 4X2-2X2-2X1.",
    "Contiene: piezas Lego de pines 4x2; 2x2; 2x1",
    "Piezas Lego de pines:\n4x2\n2x2\n2x1",
    "Además, también hay unas piezas Lego con pines 4x2 – 2x2 — 2x1",
    "Piezas Lego con tetones 4x2 - 2x2 - 2x1",
])
def test_separators_case_and_accents_do_not_change_the_result(text):
    assert _names(text) == ["Pieza Lego con pines 4x2", "Pieza Lego con pines 2x2", "Pieza Lego con pines 2x1"]


def test_y_splits_products_but_not_the_contents_of_a_box():
    assert _names("Caja con puertas, ventanas y techos y Piezas lisas 2x2") == [
        "Caja con puertas, ventanas y techos", "Pieza Lego lisa 2x2",
    ]
    assert _names("Contiene: tornillos, tuercas y arandelas", context="") == ["Tornillos", "Tuercas", "Arandelas"]


def test_brand_comes_from_the_text_or_the_container_and_is_optional():
    assert _names("Piezas con pines 2x2", context="") == ["Pieza con pines 2x2"]
    assert _names("Piezas con pines 2x2", context="Piezas Lego") == ["Pieza Lego con pines 2x2"]
    assert _names("Bases de 2 pines y vigas Technic 1x9", context="") == ["Base de 2 pines", "Viga Technic 1x9"]


def test_adjectives_are_singular_and_gender_agreed():
    assert _names("Bloques lisos 2x4 y placas transparentes 1x2", context="") == [
        "Bloque liso 2x4", "Placa transparente 1x2",
    ]
    assert _names("Piezas biseladas 2x2 y piezas curvas 1x4", context="") == [
        "Pieza con bisel 2x2", "Pieza curva 1x4",
    ]


def test_measure_before_the_subject_and_quantities_written_in_the_text():
    entries = sug.parse_description("20 piezas lisas 2x2, 10 de 2x4; 2x2 con pines")["entries"]
    assert [(e["name"], e["quantity"]) for e in entries] == [
        ("Pieza lisa 2x2", 20), ("Pieza lisa 2x4", 10), ("Pieza con pines 2x2", None),
    ]
    assert "Cantidad tomada de la descripción (20)." in entries[0]["notes"]


def test_repeated_products_are_proposed_once_with_a_warning():
    result = sug.parse_description("Pines 4x2 - 2x4\nPiezas con pines 4x2", context="Lego")
    assert [e["name"] for e in result["entries"]] == ["Pieza Lego con pines 4x2"]
    assert len(result["warnings"]) == 2
    assert "es la misma pieza que «Pieza Lego con pines 4x2»" in result["warnings"][0]
    assert "aparece más de una vez" in result["warnings"][1]


def test_a_measure_without_a_piece_type_is_flagged():
    entries = sug.parse_description("Contiene 2x2 y 4x2")["entries"]
    assert [e["name"] for e in entries] == ["Pieza 2x2", "Pieza 4x2"]
    assert all("no dice qué tipo de pieza" in " ".join(e["notes"]) for e in entries)


def test_free_products_without_measure_and_junk():
    entries = sug.parse_description("1. Sensores de color\n2. Motores grandes")["entries"]
    assert [(e["kind"], e["name"]) for e in entries] == [
        (sug.KIND_PRODUCT, "Sensores de color"), (sug.KIND_PRODUCT, "Motores grandes"),
    ]
    result = sug.parse_description("Piezas lisas 2x2 - ### - 4x2")
    assert result["unparsed"] == ["###"]
    assert "No se pudo interpretar «###»" in result["warnings"][0]


@pytest.mark.parametrize("text", [None, "", "   ", "Además,", "Contiene:", "\n\n."])
def test_empty_descriptions_propose_nothing(text):
    assert sug.parse_description(text) == {"entries": [], "warnings": [], "unparsed": []}


def test_box_names_keep_acronyms_and_brands():
    assert _names("una bolsa con sensores EV3 y cables USB de lego", context="") == [
        "Bolsa con sensores EV3 y cables USB de Lego",
    ]


@pytest.mark.parametrize("word, expected", [
    ("Piezas", "pieza"), ("pines", "pin"), ("Biseles", "bisel"), ("separadores", "separador"),
    ("bases", "base"), ("Lisas", "lisa"), ("botones", "botón"), ("cables", "cable"), ("torres", "torre"),
    ("verdes", "verde"), ("ejes", "eje"), ("tetones", "tetón"), ("gris", "gris"), ("tres", "tres"),
    ("luces", "luz"), ("pieza", "pieza"), ("4x2", "4x2"),
])
def test_singular(word, expected):
    assert sug.singular(word) == expected


def test_product_key_ignores_form_but_not_meaning():
    key = sug.product_key
    assert key("Pieza Lego con pines 4x2") == key("Piezas de Pines 2 x 4") == key("pines 4X2")
    assert key("Pieza Lego lisa 2x2") == key("Lisas 2x2")
    assert key("Pieza Lego lisa 2x2") != key("Pieza Lego con bisel 2x2")
    assert key("Pieza Lego con pines 2x2") != key("Pieza Lego con pines 2x1")
    assert key("Base Lego de 1 pin") == key("Bases de 1 pin")


# ---------------------------------------------------------------------------
# Codigos, ubicacion y productos que ya existen
# ---------------------------------------------------------------------------

def test_existing_products_are_not_duplicated_and_numbering_continues():
    children = [
        {"id": "2-1-01-01-000", "name": "Piezas de pines 4x2", "parent_id": C1["id"], "item_type": "child",
         "status": "active"},
        {"id": "2-1-01-07-000", "name": "Ruedas", "parent_id": C1["id"], "item_type": "child", "status": "active"},
    ]
    result = sug.suggest_products(C1, [C1, *children])
    assert result["existing"] == [{
        "name": "Pieza Lego con pines 4x2", "id": "2-1-01-01-000",
        "existing_name": "Piezas de pines 4x2", "source": "Piezas Lego de Pines 4x2",
    }]
    assert [(p["code"], p["name"]) for p in result["proposals"]] == [
        ("2-1-01-08-000", "Pieza Lego con pines 2x2"),
        ("2-1-01-09-000", "Pieza Lego con pines 2x1"),
        ("2-1-01-10-000", "Caja con separadores de fichas"),
        ("2-1-01-11-000", "Pieza Lego lisa 2x2"),
    ]


def test_codes_skip_retired_items_and_ignore_other_containers():
    used = [
        {"id": "2-1-03-01-000", "name": "Vieja", "parent_id": C3["id"], "status": "retired"},
        {"id": "2-1-04-05-000", "name": "Otro contenedor", "parent_id": "2-1-04-00-000", "status": "active"},
    ]
    result = sug.suggest_products(C3, [C3, *used])
    # El producto dado de baja no cuenta como existente, pero su codigo no se reutiliza.
    assert result["existing"] == []
    assert [p["code"] for p in result["proposals"]] == ["2-1-03-02-000", "2-1-03-03-000", "2-1-03-04-000"]


def test_next_child_codes_for_each_kind_of_container_code():
    assert sug.next_child_codes("2-1-05-00-000", [], 2) == (["2-1-05-01-000", "2-1-05-02-000"], "")
    assert sug.next_child_codes("2-1-05-03-000", ["2-1-05-03-001"], 2) == (["2-1-05-03-002", "2-1-05-03-003"], "")
    assert sug.next_child_codes("CONT-01", ["CONT-01-01"], 2) == (["CONT-01-02", "CONT-01-03"], "")
    codes, note = sug.next_child_codes("M1-E2", [], 2)
    assert codes == ["", ""] and "Escribe el código" in note
    codes, note = sug.next_child_codes("LAB-MICRO-01", [], 1)  # no cabe: CONT-...-NN pasaria de 13
    assert codes == [""] and "demasiado largo" in note
    codes, note = sug.next_child_codes("2-1-05-03-004", [], 1)
    assert codes == [""] and "nivel ítem" in note
    assert sug.next_child_codes("2-1-05-00-000", [], 0) == ([], "")


def test_non_gliops_container_gets_blank_codes_with_a_note():
    container = {**C3, "id": "M1-E2"}
    proposals = sug.suggest_products(container, [container])["proposals"]
    assert [p["code"] for p in proposals] == ["", "", ""]
    assert all("Escribe el código" in _notes(p) for p in proposals)
    assert all(p["location"] == C3["location"] for p in proposals)


@pytest.mark.parametrize("location, code, expected", [
    ("Estantería- 2; Piso-1; Contenedor-1.", "2-1-01-04-000", "Estantería- 2; Piso-1; Contenedor-1; Caja 04."),
    ("Estante 3, piso 2", "2-1-01-12-000", "Estante 3, piso 2, Caja 12"),
    ("Bodega", "2-1-01-01-000", "Bodega · Caja 01"),
    ("", "2-1-01-02-000", "Estantería 2; Piso 1; Contenedor 01; Caja 02"),
    ("Estantería- 2; Piso-1; Contenedor-1.", "", "Estantería- 2; Piso-1; Contenedor-1."),
])
def test_child_location(location, code, expected):
    assert sug.child_location({"id": "2-1-01-00-000", "location": location}, code) == expected


def test_child_location_inside_a_box_level_container():
    box = {"id": "2-1-01-03-000", "location": "Estantería 2; Caja 03"}
    assert sug.child_location(box, "2-1-01-03-002") == "Estantería 2; Caja 03; Ítem 002"


# ---------------------------------------------------------------------------
# Vista previa editable y validacion antes de crear
# ---------------------------------------------------------------------------

def _rows(container=C1, items=None):
    proposals = sug.suggest_products(container, items or [container])["proposals"]
    return sug.rows_from_records(sug.editor_records(proposals), proposals), proposals


def test_editor_records_roundtrip():
    rows, proposals = _rows(C2)
    records = sug.editor_records(proposals)
    assert list(records[0]) == list(sug.EDITOR_COLUMNS)
    assert records[0] == {
        "Crear": True, "Código": "2-1-02-01-000", "Nombre": "Pieza Lego con pines 6x2",
        "Característica": "Con pines", "Cantidad": 0, "Unidad": "unidad", "Notas": "",
    }
    assert records[-1]["Crear"] is False and "dimensión 0" in records[-1]["Notas"]
    assert rows[1] == {
        "selected": True, "code": "2-1-02-02-000", "name": "Pieza Lego con pines 6x1", "quantity": 0,
        "unit": "unidad", "source": "Piezas Lego de Pines … 6x1", "feature": "Con pines",
    }


def test_valid_rows_become_payloads_ready_for_storage():
    rows, _ = _rows()
    rows[0]["quantity"] = 35
    rows[1]["quantity"] = float("nan")       # celda borrada en el editor = pendiente
    rows[2]["selected"] = False
    rows[3]["unit"] = "  "
    check = sug.validate_rows(rows, C1, [C1])
    assert check["errors"] == [] and check["warnings"] == [] and check["selected"] == 4
    assert [p["id"] for p in check["payloads"]] == ["2-1-01-01-000", "2-1-01-02-000", "2-1-01-04-000", "2-1-01-05-000"]
    first, second = check["payloads"][:2]
    assert first == {
        "id": "2-1-01-01-000", "name": "Pieza Lego con pines 4x2", "category": "Piezas Lego",
        "description": "Creado desde la descripción de «Contenedor 1» (2-1-01-00-000): «Piezas Lego de Pines 4x2».",
        "item_type": "child", "parent_id": "2-1-01-00-000", "unit": "unidad", "quantity": 35,
        "location": "Estantería- 2; Piso-1; Contenedor-1; Caja 01.", "min_stock_alert": 0, "status": "active",
    }
    assert second["quantity"] == 0 and second["description"].endswith(sug.PENDING_COUNT_NOTE)
    assert check["payloads"][2]["unit"] == "unidad"


def test_validation_reports_every_problem_with_its_row():
    rows, _ = _rows()
    existing = {"id": "2-1-01-09-000", "name": "Rueda", "parent_id": C1["id"], "item_type": "child",
                "status": "active"}
    rows[0].update(name="  ")
    rows[1].update(code="")
    rows[2].update(code="2-1-01-01-001x")
    rows[3].update(code="2-1-02-04-000")            # otro contenedor
    rows[4].update(code="2-1-01-09-000", quantity=-1)
    check = sug.validate_rows(rows, C1, [C1, existing])
    assert check["selected"] == 5 and len(check["payloads"]) == 0
    errors = check["errors"]
    assert len(errors) == 5 and all(e.endswith(".") and not e.endswith("..") for e in errors)
    assert errors[0] == "Fila 1: el nombre es obligatorio."
    assert errors[1] == "Fila 2 (Pieza Lego con pines 2x2): el código es obligatorio."
    assert errors[2].startswith("Fila 3 (Pieza Lego con pines 2x1): El codigo '2-1-01-01-001x' tiene 14 caracteres")
    assert errors[3] == ("Fila 4 (Caja con separadores de fichas): el código 2-1-02-04-000 no queda dentro "
                         "del contenedor 2-1-01-00-000 (usa 2-1-01-NN-000).")
    assert errors[4] == ("Fila 5 (Pieza Lego lisa 2x2): el código 2-1-01-09-000 ya pertenece a «Rueda»; "
                         "la cantidad no puede ser negativa.")


def test_validation_blocks_duplicates_in_the_batch_and_in_the_container():
    rows, _ = _rows()
    rows[1].update(code=rows[0]["code"])
    rows[4].update(name="Piezas Lego de pines 2X4")   # misma pieza que la fila 1
    child = {"id": "2-1-01-20-000", "name": "Caja con separadores de fichas", "parent_id": C1["id"],
             "item_type": "child", "status": "active"}
    errors = sug.validate_rows(rows, C1, [C1, child])["errors"]
    assert "el código 2-1-01-01-000 está repetido (fila 1)" in errors[0]
    assert "ya existe «Caja con separadores de fichas» en este contenedor (2-1-01-20-000)" in errors[1]
    assert "repite el producto de la fila 1" in errors[2]


def test_validation_rejects_the_container_code_and_non_integer_quantities():
    rows, _ = _rows(C3, [C3])
    rows[0].update(code=C3["id"])
    rows[1].update(quantity=2.5)
    rows[2].update(quantity="tres")
    errors = sug.validate_rows(rows, C3, [C3])["errors"]
    assert "no puede usar el mismo código del contenedor" in errors[0]
    assert "número entero" in errors[1] and "número entero" in errors[2]


def test_selecting_the_1x0_row_without_fixing_it_warns():
    rows, _ = _rows(C2)
    rows[-1]["selected"] = True
    check = sug.validate_rows(rows, C2, [C2])
    assert check["errors"] == []
    assert check["warnings"] == ["Fila 13 (Pieza Lego con bisel 1x0): la medida «1x0» tiene una dimensión 0; "
                                 "¿quisiste decir «1x1»?"]
    rows[-1]["name"] = "Pieza Lego con bisel 1x1"
    assert sug.validate_rows(rows, C2, [C2])["warnings"] == []


@pytest.mark.parametrize("value, expected", [
    (True, True), (False, False), (None, False), (float("nan"), False), ("true", True), ("no", False), (1, True),
])
def test_selected_flag_tolerates_editor_values(value, expected):
    assert sug._truthy(value) is expected


def test_retired_rows_and_other_containers_do_not_block():
    rows, _ = _rows(C3, [C3])
    retired = {"id": "2-1-03-01-000", "name": "Pieza Lego con pines 4x2", "parent_id": C3["id"],
               "item_type": "child", "status": "retired"}
    other = {"id": "2-1-01-01-000", "name": "Pieza Lego con pines 3x2", "parent_id": C1["id"],
             "item_type": "child", "status": "active"}
    check = sug.validate_rows(rows, C3, [C3, retired, other])
    assert check["errors"] == [] and len(check["payloads"]) == 3


def test_is_pending_count():
    payload = sug.validate_rows(_rows(C3, [C3])[0], C3, [C3])["payloads"][0]
    assert sug.is_pending_count(payload)
    assert not sug.is_pending_count({**payload, "quantity": 4})
    assert not sug.is_pending_count({**payload, "description": "Otra cosa"})
    assert not sug.is_pending_count({**C1, "description": sug.PENDING_COUNT_NOTE})
    assert not sug.is_pending_count({})


# ---------------------------------------------------------------------------
# LabStorage.save_items_bulk (base aislada en tmp por conftest)
# ---------------------------------------------------------------------------

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


def default_payloads(container, storage):
    """Lo que crearia el generador si el usuario confirma la propuesta sin cambios."""
    items = storage.get_all_items(include_retired=True)
    proposals = sug.suggest_products(container, items)["proposals"]
    rows = sug.rows_from_records(sug.editor_records(proposals), proposals)
    check = sug.validate_rows(rows, container, items)
    assert check["errors"] == []
    return check["payloads"]


_payloads = default_payloads


def test_bulk_creates_every_product_with_a_single_write(db, writes):
    payloads = _payloads(C2, db)
    created = db.save_items_bulk(payloads, actor_email=ACTOR,
                                 details=sug.HISTORY_DETAILS.format(parent_id=C2["id"]))
    assert created == [p["id"] for p in payloads] and len(created) == 12
    assert len(writes) == 1                                 # un solo commit en GitHub

    children = {c["id"]: c for c in db.get_children(C2["id"])}
    assert set(children) == set(created)
    base = children["2-1-02-04-000"]
    assert base["name"] == "Base Lego de 1 pin" and base["quantity"] == 0 and base["unit"] == "unidad"
    assert base["location"] == "Estantería- 2; Piso-1; Contenedor-2; Caja 04."
    assert base["created_by"] == ACTOR and base["status"] == "active" and base["category"] == "Piezas Lego"
    assert sug.is_pending_count(base)
    history = db.get_item_history("2-1-02-04-000")
    assert [(h["type"], str(h["quantity_change"]), h["actor_user_id"]) for h in history] == [("Alta", "0", ACTOR)]
    assert history[0]["details"] == "Creado desde la descripción del contenedor 2-1-02-00-000 (generador de productos)."
    # Lo creado ya no se vuelve a proponer.
    assert sug.suggest_products(C2, db.get_all_items())["proposals"][0]["name"] == "Pieza Lego con bisel 1x0"


def test_bulk_is_all_or_nothing(db, writes):
    payloads = _payloads(C1, db)
    payloads[1] = {**payloads[1], "id": "2-1-01-00-001"}           # item sin caja: GLIOPS invalido
    payloads[2] = {**payloads[2], "quantity": -3}
    payloads[3] = {**payloads[3], "parent_id": "2-1-09-00-000"}    # contenedor inexistente
    payloads[4] = {**payloads[4], "id": payloads[0]["id"]}         # repetido en la tanda
    with pytest.raises(ValueError) as excinfo:
        db.save_items_bulk(payloads, actor_email=ACTOR)
    message = str(excinfo.value)
    assert message.startswith("No se creo ningun item.")
    assert "Fila 2 (2-1-01-00-001)" in message and "Item debe ser 000" in message
    assert "Fila 3" in message and "mayor o igual a 0" in message
    assert "Fila 4" in message and "no existe" in message
    assert "Fila 5" in message and "repetido en la misma tanda" in message
    assert writes == [] and db.get_children(C1["id"]) == []


def test_bulk_refuses_codes_in_use_and_reuses_retired_ones(db):
    payloads = _payloads(C3, db)
    db.save_items_bulk(payloads[:1], actor_email=ACTOR)
    with pytest.raises(ValueError, match="Ya existe un item con ese codigo"):
        db.save_items_bulk(payloads, actor_email=ACTOR)

    db.retire_item(payloads[0]["id"], actor_email=ACTOR)
    created = db.save_items_bulk(payloads, actor_email=ACTOR)
    assert created == [p["id"] for p in payloads]
    history = db.get_item_history(payloads[0]["id"])
    assert [h["type"] for h in history] == ["Alta"]          # el rastro anterior se borro


def test_bulk_validates_parents_and_types(db):
    db.retire_item(C3["id"], actor_email=ACTOR)
    with pytest.raises(ValueError, match="dado de baja"):
        db.save_items_bulk([{"id": "2-1-03-09-000", "name": "X", "item_type": "child", "parent_id": C3["id"]}])
    with pytest.raises(ValueError, match="Tipo de item invalido"):
        db.save_items_bulk([{"id": "LAB-1", "name": "X", "item_type": "kit"}])
    with pytest.raises(ValueError, match="Solo un Contenedor de Característica"):
        db.save_items_bulk([{"id": "LAB-1", "name": "X", "item_type": "standalone", "parent_id": C1["id"]}])
    with pytest.raises(ValueError, match="codigo y nombre son obligatorios"):
        db.save_items_bulk([{"id": "LAB-1", "name": " "}])
    assert db.get_item("LAB-1") is None


def test_bulk_accepts_a_container_created_in_the_same_batch(db, writes):
    created = db.save_items_bulk([
        {"id": "2-2-01-00-000", "name": "Contenedor nuevo", "item_type": "master"},
        {"id": "2-2-01-01-000", "name": "Pieza nueva", "item_type": "child", "parent_id": "2-2-01-00-000",
         "quantity": "7", "min_stock_alert": 2.0, "unit": "", "details": "Detalle propio."},
    ], actor_email=ACTOR)
    assert created == ["2-2-01-00-000", "2-2-01-01-000"] and len(writes) == 1
    child = db.get_item("2-2-01-01-000")
    assert (child["quantity"], child["min_stock_alert"], child["unit"]) == (7, 2, "unidad")
    assert db.get_item_history("2-2-01-01-000")[0]["details"] == "Detalle propio."
    assert db.get_item_history("2-2-01-00-000")[0]["details"] == "Item creado en el sistema."


def test_bulk_with_nothing_to_create_does_not_write(db, writes):
    assert db.save_items_bulk([], actor_email=ACTOR) == []
    assert db.save_items_bulk(None) == []
    assert writes == []


def test_bulk_rejects_non_integer_quantities(db):
    for bad in ("2.5", "muchas", math.inf):
        with pytest.raises(ValueError):
            db.save_items_bulk([{"id": "LAB-9", "name": "X", "quantity": bad}])
