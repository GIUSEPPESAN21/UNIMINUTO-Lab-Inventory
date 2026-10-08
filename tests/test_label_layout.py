# -*- coding: utf-8 -*-
"""Diseño adaptable de etiquetas: tamaño real configurable, piso de legibilidad,
espacio visible entre bloques, contenido priorizado (se omite lo menos
importante en vez de apretar las letras), PDF sin escalado, hoja de prueba con
regla milimetrada y configuracion guardada en la base."""

import io
import json
import re
import time

import pandas as pd
import pytest
from PIL import Image

from core import labels
from core import storage as storage_module
from test_labels import _scan_label

PRESETS = [(50, 25), (50, 30), (60, 40), (100, 50), (100, 100), (100, 150)]
ITEMS = [
    {"id": "2-1-01-00-000", "name": "Contenedor 1", "item_type": "master"},
    {"id": "2-1-01-01-000", "name": "Resistencias 220 ohm", "item_type": "child",
     "category": "Electrónica", "location": "Estante 2"},
    {"id": "2-1-01-01-001", "name": "Multímetro digital Fluke 115 True RMS", "item_type": "standalone",
     "category": "Instrumentación", "location": "Laboratorio 3"},
    {"id": "M1-E2", "name": "Impresora 3D Prusa MK4", "item_type": "standalone", "category": "Fabricación"},
    {"id": "E3-LM07", "name": "Brazo robótico Lego", "item_type": "standalone"},
    {"id": "LAB-MIC-01", "name": "Microscopio óptico binocular", "item_type": "standalone",
     "category": "Óptica", "location": "Estantería 2 · Piso 1"},
    {"id": "12345678901234567890", "name": "Osciloscopio Rigol", "item_type": "standalone"},
    {"id": "ABCDEFGHIJKLM", "name": "Supercalifragilisticoespialidoso", "item_type": "standalone"},
]
FULL_ITEM = ITEMS[2]


def _dots(mm, dpi=labels.LABEL_DPI):
    return mm * dpi / 25.4


def _blocks(layout):
    """Bloques verticales en orden: (tipo, y0, y1)."""
    divider_top = layout.divider_box[1] if layout.divider_box else None
    header = [
        line for line in layout.lines
        if divider_top is not None and line.top < divider_top and line.role != "name"
    ]
    blocks = []
    if header or layout.logo_box:
        tops = [line.top for line in header] + ([layout.logo_box[1]] if layout.logo_box else [])
        bottoms = [line.top + line.ink_height for line in header] + (
            [layout.logo_box[3]] if layout.logo_box else []
        )
        blocks.append(("header", min(tops), max(bottoms)))
    if layout.divider_box:
        blocks.append(("divider", layout.divider_box[1], layout.divider_box[3]))
    if layout.band_box:
        blocks.append(("band", layout.band_box[1], layout.band_box[3]))
    for line in layout.lines_for("meta"):
        if divider_top is None or line.top > divider_top:
            blocks.append(("meta", line.top, line.top + line.ink_height))
    blocks.append(("bars", layout.bar_box[1], layout.bar_box[3]))
    code = layout.lines_for("code")[0]
    blocks.append(("code", code.top, code.top + code.ink_height))
    return blocks


# --- Espaciado y margenes -------------------------------------------------------

@pytest.mark.parametrize("size", PRESETS)
@pytest.mark.parametrize("item", ITEMS, ids=[i["id"] for i in ITEMS])
def test_blocks_never_touch_and_stay_inside_the_margins(size, item):
    spec = labels.LabelSpec(*size)
    layout = labels.layout_item_label(item, spec)
    width, height = spec.canvas_size
    blocks = _blocks(layout)

    for (kind_a, _, end_a), (kind_b, start_b, _) in zip(blocks, blocks[1:]):
        # Antes habia 1-2 puntos (0,1-0,25 mm) entre lineas; ahora >= 0,37 mm.
        assert start_b - end_a >= 3, f"{kind_a} pegado a {kind_b} en {spec.size_text}"
    assert blocks[0][1] >= _dots(1.4)
    assert blocks[-1][2] <= height - _dots(1.4)
    for line in layout.lines:
        x0, _, x1, _ = line.box
        assert x0 >= _dots(1.4) - 1 and x1 <= width - _dots(1.4) + 1, line.text


@pytest.mark.parametrize("size", PRESETS)
def test_header_lines_keep_air_between_them(size):
    layout = labels.layout_item_label(FULL_ITEM, labels.LabelSpec(*size))
    header = [line for line in layout.lines if line.top < layout.divider_box[1]]
    for first, second in zip(header, header[1:]):
        assert second.top - (first.top + first.ink_height) >= 3


def test_pixels_confirm_blank_rows_between_text_blocks():
    """Comprobacion independiente del motor: en la imagen impresa hay filas
    totalmente blancas entre cada bloque (nada se toca)."""
    layout = labels.layout_item_label(ITEMS[1])
    img = labels.render_label(layout).convert("L")
    width = img.width
    blank = [all(img.getpixel((x, y)) > 127 for x in range(width)) for y in range(img.height)]
    for (_, _, end_a), (_, start_b, _) in zip(_blocks(layout), _blocks(layout)[1:]):
        assert any(blank[end_a:start_b]), "no hay una sola fila en blanco entre dos bloques"


# --- Legibilidad --------------------------------------------------------------------

@pytest.mark.parametrize("dpi", labels.SUPPORTED_DPI)
@pytest.mark.parametrize("size", PRESETS)
def test_no_text_is_printed_below_the_legibility_floor(size, dpi):
    spec = labels.LabelSpec(*size, dpi)
    for item in ITEMS:
        layout = labels.layout_item_label(item, spec)
        for line in layout.lines:
            assert line.pt(dpi) >= labels._STYLES[line.role].floor_pt, (item["id"], line)
        assert layout.min_text_pt >= 5.0
        assert layout.lines_for("name")[0].pt(dpi) >= 7.0
        assert layout.lines_for("code")[0].pt(dpi) >= 6.0


@pytest.mark.parametrize("size", PRESETS)
def test_institutional_texts_and_codes_are_never_truncated(size):
    spec = labels.LabelSpec(*size)
    for item in ITEMS:
        layout = labels.layout_item_label(item, spec)
        assert [line.text for line in layout.lines_for("code")] == [item["id"]]
        for line in layout.lines_for("lab"):
            assert line.text == labels.LABEL_INSTITUTION_TEXT
        for line in layout.lines_for("notice"):
            assert line.text in (labels.LABEL_NOTICE_TEXT, labels.LABEL_NOTICE_SHORT_TEXT)
        routes = [
            chunk for line in layout.lines
            for chunk in line.text.split(labels._META_SEPARATOR) if chunk.startswith("RUTA:")
        ]
        for route in routes:  # la ruta puede compartir linea, pero siempre va completa
            assert route == labels._location_guide(item["id"])


def test_long_notice_uses_the_short_variant_instead_of_an_ellipsis():
    small = labels.layout_label("LAB-MIC-01", labels.LabelSpec(60, 40), description="Microscopio")
    assert [line.text for line in small.lines_for("notice")] == [labels.LABEL_NOTICE_SHORT_TEXT]
    wide = labels.layout_label("LAB-MIC-01", labels.LabelSpec(100, 50), description="Microscopio")
    assert [line.text for line in wide.lines_for("notice")] == [labels.LABEL_NOTICE_TEXT]


def test_bigger_labels_print_bigger_text_and_more_content():
    small = labels.layout_item_label(FULL_ITEM, labels.LabelSpec(50, 25))
    big = labels.layout_item_label(FULL_ITEM, labels.LabelSpec(100, 50))
    assert big.lines_for("code")[0].size > small.lines_for("code")[0].size
    assert big.min_text_pt > small.min_text_pt
    assert len(big.omitted) < len(small.omitted)
    assert big.omitted == ()
    assert "name" in small.shortened and "name" not in big.shortened


def test_small_labels_omit_low_priority_content_and_say_so():
    layout = labels.layout_item_label(FULL_ITEM, labels.LabelSpec(50, 25))
    assert {"notice", "type", "category"} <= set(layout.omitted)
    assert "brand" in layout.shown and "route" in layout.shown
    names = labels.describe_omitted(layout)
    assert "Categoría" in names and all(isinstance(n, str) and n for n in names)


@pytest.mark.parametrize("size", PRESETS)
def test_brand_is_kept_on_every_preset(size):
    for item in ITEMS:
        assert "brand" in labels.layout_item_label(item, labels.LabelSpec(*size)).shown


def test_long_names_wrap_instead_of_being_cut_when_there_is_room():
    layout = labels.layout_item_label(FULL_ITEM, labels.LabelSpec(60, 40))
    names = [line.text for line in layout.lines_for("name")]
    assert len(names) == 2 and " ".join(names) == FULL_ITEM["name"]


def test_content_can_be_switched_off():
    bare = labels.LabelSpec(content=())
    layout = labels.layout_item_label(FULL_ITEM, bare)
    assert layout.logo_box is None and layout.divider_box is None
    assert {line.role for line in layout.lines} == {"name", "code"}
    assert layout.omitted == ()

    notice_only = labels.layout_item_label(FULL_ITEM, labels.LabelSpec(content=("notice",)))
    assert notice_only.logo_box is None
    assert [line.role for line in notice_only.lines][:1] == ["notice"]


@pytest.mark.parametrize("size", PRESETS)
@pytest.mark.parametrize("dpi", labels.SUPPORTED_DPI)
def test_every_size_and_resolution_scans_back_exactly(size, dpi):
    spec = labels.LabelSpec(*size, dpi)
    for item in ITEMS:
        img = labels.render_label(labels.layout_item_label(item, spec))
        assert img.size == spec.canvas_size and img.mode == "1"
        scan = _scan_label(img)
        assert scan["text"] == item["id"]
        assert scan["dots"] * 25.4 / dpi >= 0.249           # modulo >= 0,25 mm
        assert scan["band_height"] * 25.4 / dpi >= 6.99     # barras >= 7 mm
        assert scan["quiet_left"] >= 6 * scan["dots"] and scan["quiet_right"] >= 6 * scan["dots"]


def test_300dpi_png_declares_its_resolution():
    spec = labels.LabelSpec(50, 25, 300)
    data = labels.generate_item_label_png_bytes(FULL_ITEM, spec)
    img = Image.open(io.BytesIO(data))
    assert img.size == (590, 295)
    assert img.info["dpi"] == pytest.approx((300, 300), abs=0.5)


def test_code_too_long_for_a_narrow_label_is_reported_clearly():
    with pytest.raises(ValueError, match="no cabe"):
        labels.layout_label("12345678901234567890", labels.LabelSpec(20, 15), description="X")


# --- Hoja de prueba ----------------------------------------------------------------

@pytest.mark.parametrize("size, dpi", [((50, 25), 203), ((100, 50), 203), ((20, 15), 203), ((50, 25), 300)])
def test_calibration_page_has_a_full_frame_and_a_ruler_of_exact_length(size, dpi):
    spec = labels.LabelSpec(*size, dpi)
    img = labels.generate_calibration_image(spec).convert("L")
    width, height = img.size
    assert (width, height) == spec.canvas_size
    pix = img.load()

    def longest_run(y):
        best = run = 0
        for x in range(width):
            run = run + 1 if pix[x, y] < 128 else 0
            best = max(best, run)
        return best

    runs = [longest_run(y) for y in range(height)]
    inset = round(_dots(1.0, dpi))
    frame = width - 2 * inset
    assert runs[inset] >= frame - 2 and runs[height - 1 - inset] >= frame - 2  # marco completo
    ruler = max(run for run in runs if run < frame - 2)
    expected_mm = max(10, int((size[0] - 8) // 10) * 10)
    assert abs(ruler - _dots(expected_mm, dpi)) <= 3  # la regla mide lo que dice (+-0,4 mm)


def test_calibration_pdf_is_print_ready():
    spec = labels.LabelSpec(100, 50)
    data = labels.generate_calibration_pdf_bytes(spec)
    assert data.startswith(b"%PDF-1.7") and b"/PrintScaling /None" in data
    media_box = re.search(rb"/MediaBox \[0 0 ([0-9.]+) ([0-9.]+)\]", data)
    assert float(media_box.group(1)) * 25.4 / 72 == pytest.approx(100, abs=0.01)
    assert float(media_box.group(2)) * 25.4 / 72 == pytest.approx(50, abs=0.01)


# --- Especificacion y configuracion guardada ----------------------------------------------

def test_spec_validates_ranges_with_helpful_messages():
    with pytest.raises(ValueError, match="ancho"):
        labels.LabelSpec(10, 25)
    with pytest.raises(ValueError, match="104 mm"):
        labels.LabelSpec(120, 25)
    with pytest.raises(ValueError, match="alto"):
        labels.LabelSpec(50, 10)
    with pytest.raises(ValueError, match="203 dpi"):
        labels.LabelSpec(50, 25, 600)
    with pytest.raises(ValueError, match="números"):
        labels.LabelSpec("ancho", 25)


def test_spec_normalizes_content_and_round_trips():
    spec = labels.LabelSpec(50, 30, 203, ("category", "brand", "desconocido"))
    assert spec.content == ("brand", "category")
    assert labels.LabelSpec.from_dict(spec.to_dict()) == spec
    assert spec.preset_key == "50x30"
    assert labels.LabelSpec(55, 35).preset_key == "custom"
    assert spec.describe() == "50 × 30 mm · 203 dpi"
    assert labels.LabelSpec(50.5, 25).size_text == "50,5 × 25 mm"
    with pytest.raises(ValueError):
        labels.LabelSpec.from_dict({"content": "brand"})


def test_load_label_spec_falls_back_to_the_factory_default(storage):
    key = labels.LABEL_SPEC_SETTING_KEY
    assert labels.load_label_spec(storage) == labels.LabelSpec()
    storage.set_setting(key, "{no es json")
    assert labels.load_label_spec(storage) == labels.LabelSpec()
    storage.set_setting(key, json.dumps({"width_mm": 500}))
    assert labels.load_label_spec(storage) == labels.LabelSpec()
    assert labels.load_label_spec(object()) == labels.LabelSpec()  # backend sin configuracion


def test_saved_spec_is_loaded_back(storage):
    spec = labels.LabelSpec(100, 50, 203, ("brand", "route"))
    labels.save_label_spec(storage, spec, actor_email="prof@uniminuto.edu.co")
    assert labels.load_label_spec(storage) == spec


def test_settings_persist_in_the_database_and_survive_a_restart():
    db = storage_module.LabStorage()
    assert db.get_setting("label_spec") is None
    assert db.get_setting("label_spec", "defecto") == "defecto"
    db.set_setting("label_spec", '{"width_mm": 100}', "prof@uniminuto.edu.co")
    db.set_setting("label_spec", '{"width_mm": 60}', "prof@uniminuto.edu.co")  # reemplaza
    storage_module._cached_dfs = None  # = reiniciar la app
    assert db.get_setting("label_spec") == '{"width_mm": 60}'
    settings = storage_module._load_cache()["settings"]
    assert len(settings) == 1 and settings.iloc[0]["updated_by"] == "prof@uniminuto.edu.co"


def test_databases_from_before_this_version_get_the_settings_sheet():
    with pd.ExcelWriter(storage_module.EXCEL_PATH, engine="openpyxl") as writer:
        for sheet, columns in storage_module.SHEET_COLUMNS.items():
            if sheet != "settings":
                pd.DataFrame(columns=columns).to_excel(writer, sheet_name=sheet, index=False)
    storage_module._cached_dfs = None
    db = storage_module.LabStorage()
    assert "settings" in pd.ExcelFile(storage_module.EXCEL_PATH, engine="openpyxl").sheet_names
    assert db.get_setting("label_spec", "defecto") == "defecto"


# --- Rendimiento y cache ------------------------------------------------------------------

def test_item_label_files_are_cached_per_item_and_size():
    first = labels.item_label_files(ITEMS[1], labels.LabelSpec())
    again = labels.item_label_files(dict(ITEMS[1]), labels.LabelSpec())
    assert first is again
    bigger = labels.item_label_files(ITEMS[1], labels.LabelSpec(100, 50))
    assert bigger != first
    media_box = re.search(rb"/MediaBox \[0 0 ([0-9.]+) ([0-9.]+)\]", bigger[0])
    assert float(media_box.group(1)) * 25.4 / 72 == pytest.approx(100, abs=0.01)
    assert labels.item_label_files({"id": "CAJA 001"}) == (None, None)


def test_labels_are_fast_enough_for_a_large_catalog():
    items = [
        {"id": f"2-1-0{1 + i % 9}-0{1 + i % 5}-00{1 + i % 9}", "name": f"Equipo de prueba número {i}",
         "category": "Electrónica", "location": "Estante 2", "item_type": "standalone"}
        for i in range(30)
    ]
    labels.layout_item_label(items[0])  # calentamiento de fuentes
    start = time.perf_counter()
    for item in items:
        labels.render_label(labels.layout_item_label(item))
    # ~15 ms por etiqueta en un equipo normal; el margen amplio evita falsos fallos en CI.
    assert (time.perf_counter() - start) / len(items) < 0.25
