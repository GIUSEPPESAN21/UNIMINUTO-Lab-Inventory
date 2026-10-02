# -*- coding: utf-8 -*-
"""Pruebas de core/labels.py: etiqueta profesional de 50x25mm para una
termica SAT TT 460 (logo, identificacion del producto, Code 128 y codigo
legible). El codigo de barras se lee desde los pixeles, como lo haria un
lector, para comprobar que devuelve EXACTAMENTE el texto registrado.
"""

import io
import itertools
import random
import re
import string

import pytest
from barcode.charsets import code128 as c128
from PIL import Image

from core import barcode, labels

STOP_PATTERN = "1100011101011"  # parada completa segun ISO/IEC 15417 (anchos 2331112)
PATTERN_TO_VALUE = {pattern: value for value, pattern in enumerate(c128.CODES)}

LONG_DESCRIPTION = "Microscopio óptico binocular Olympus CX23 con cámara y 4 objetivos"
PRODUCT = {
    "id": "LAB-MIC-01",
    "name": "Microscopio óptico binocular",
    "category": "Óptica",
    "location": "Estantería 2 · Piso 1",
    "item_type": "standalone",
}

DECODE_CASES = [
    "1-2-05-12-001", "M1-E2", "E3-LM07", "3-6-999-999-9999",            # GLIOPS V3
    "0012345", "7", "42", "9912345", "12345678901234567890", "1234567890123456789",
    "LAB-MIC-01", "lab-mic-01", "CAJA-001", "SENSOR_T.02", "99-LAB",
    "X1234Y", "AB12345CD", "ABCDEFGHIJKLM", "A1B2C3D4E5F6G",
]


# --- Lector Code 128 minimo, independiente del codificador de la app ---------

def _values_to_text(symbols):
    start, *data = symbols
    charset = {103: "A", 104: "B", 105: "C"}[start]
    out = []
    for value in data:
        if charset == "C":
            if value < 100:
                out.append(f"{value:02d}")
            elif value == 100:
                charset = "B"
            elif value == 101:
                charset = "A"
            else:
                raise AssertionError(f"valor inesperado en el subconjunto C: {value}")
        elif charset == "B":
            if value < 95:
                out.append(chr(value + 32))
            elif value == 99:
                charset = "C"
            elif value == 101:
                charset = "A"
            else:
                raise AssertionError(f"valor inesperado en el subconjunto B: {value}")
        else:
            if value < 64:
                out.append(chr(value + 32))
            elif value < 96:
                out.append(chr(value - 64))
            elif value == 99:
                charset = "C"
            elif value == 100:
                charset = "B"
            else:
                raise AssertionError(f"valor inesperado en el subconjunto A: {value}")
    return "".join(out)


def _decode_modules(modules):
    """Texto de un patron de modulos Code 128 (de la barra de inicio a la de parada)."""
    assert modules.endswith(STOP_PATTERN), "falta el patron de parada"
    body = modules[: -len(STOP_PATTERN)]
    assert body and len(body) % 11 == 0, "simbolos incompletos"
    values = [PATTERN_TO_VALUE[body[i:i + 11]] for i in range(0, len(body), 11)]
    *symbols, checksum = values
    expected = (symbols[0] + sum(pos * v for pos, v in enumerate(symbols[1:], start=1))) % 103
    assert checksum == expected, "checksum invalido"
    return _values_to_text(symbols)


def _scan_label(img):
    """Lee la etiqueta como un lector: toma la franja de barras (la racha mas
    alta de filas de pixeles identicas y con tinta), la recorre de izquierda a
    derecha y decodifica. Devuelve el texto y la geometria medida."""
    gray = img.convert("L")
    width, height = gray.size
    pix = gray.load()
    rows = [tuple(pix[x, y] < 128 for x in range(width)) for y in range(height)]

    band_top, band_height, y = 0, 0, 0
    while y < height:
        if not any(rows[y]):
            y += 1
            continue
        start = y
        while y < height and rows[y] == rows[start]:
            y += 1
        if y - start > band_height:
            band_top, band_height = start, y - start

    row = rows[band_top]
    first = row.index(True)
    last = width - 1 - row[::-1].index(True)
    runs = [len(list(group)) for _, group in itertools.groupby(row[first:last + 1])]
    dots = min(runs)
    assert all(run % dots == 0 for run in runs), "barras con un ancho que no es un numero entero de modulos"
    modules = "".join(("1" if i % 2 == 0 else "0") * (run // dots) for i, run in enumerate(runs))
    return {
        "text": _decode_modules(modules), "dots": dots,
        "quiet_left": first, "quiet_right": width - 1 - last,
        "band_top": band_top, "band_height": band_height, "rows": rows,
    }


def _text_lines(rows, y0, y1):
    """Cantidad de lineas de texto (grupos de filas con tinta) entre y0 y y1."""
    lines, inside = 0, False
    for y in range(y0, y1):
        has_ink = any(rows[y])
        if has_ink and not inside:
            lines += 1
        inside = has_ink
    return lines


def _ink_x_bounds(rows, y0, y1):
    xs = [x for y in range(y0, y1) for x, black in enumerate(rows[y]) if black]
    return min(xs), max(xs)


def _region_ink(rows, x0, y0, x1, y1):
    return sum(rows[y][x] for y in range(y0, y1) for x in range(x0, x1))

# --- Pruebas -------------------------------------------------------------------

def test_code128_table_matches_iso_standard():
    """El lector usa la tabla de patrones de python-barcode; se ancla aqui a
    valores del estandar para no depender de ella a ciegas."""
    assert c128.CODES[0] == "11011001100"      # 212222
    assert c128.CODES[103] == "11010000100"    # START A, 211412
    assert c128.CODES[104] == "11010010000"    # START B, 211214
    assert c128.CODES[105] == "11010011100"    # START C, 211232
    assert c128.STOP + "11" == STOP_PATTERN


@pytest.mark.parametrize(
    "code", ["1-2-05-12-001", "M1-E2", "E3-LM07", "3-6-999-999-9999", "0012345", "LAB-MIC-01", "CAJA-001"]
)
def test_generate_label_image_has_fixed_canvas_size(code):
    img = labels.generate_label_image(code)
    assert img.size == labels.LABEL_CANVAS_SIZE
    assert img.mode == "1"  # blanco y negro puro, ideal para termica


def test_generate_label_image_custom_canvas_size():
    img = labels.generate_label_image("M1-E2", canvas_size=(300, 150))
    assert img.size == (300, 150)
    small = labels.generate_label_image("LAB-MIC-01", canvas_size=(300, 150), description=LONG_DESCRIPTION)
    assert _scan_label(small)["text"] == "LAB-MIC-01"


@pytest.mark.parametrize("code", ["LAB MIC 01", "LAB-MÍC-01", "M3-E1", "ABCDEFGHIJKLMN", ""])
def test_generate_label_image_rejects_invalid_code(code):
    with pytest.raises(ValueError):
        labels.generate_label_image(code)


@pytest.mark.parametrize("code", DECODE_CASES)
def test_label_barcode_scans_back_to_the_exact_code(code):
    scan = _scan_label(labels.generate_label_image(code, description=LONG_DESCRIPTION))
    assert scan["text"] == code
    assert scan["quiet_left"] >= 6 * scan["dots"]
    assert scan["quiet_right"] >= 6 * scan["dots"]


@pytest.mark.parametrize(
    "code",
    ["1-2-05-12-001", "0012345", "LAB-MIC-01", "ABCDEFGHIJKLM", "A1B2C3D4E5F6G",
     "12345678901234567890", "1234567890123456789"],
)
def test_label_bars_are_at_least_025mm_wide_and_9mm_tall(code):
    scan = _scan_label(labels.generate_label_image(code, description=LONG_DESCRIPTION))
    assert scan["dots"] >= 2          # 2 puntos a 203 dpi = 0,25 mm
    assert scan["band_height"] >= 72  # 9 mm a 203 dpi


def _random_free_codes(count=400, seed=20260928):
    rng = random.Random(seed)
    codes = set()
    while len(codes) < count:
        if rng.random() < 0.3:
            code = "".join(rng.choice(string.digits) for _ in range(rng.randint(1, barcode.NUMERIC_MAX_LEN)))
        else:
            parts = []
            for _ in range(rng.randint(1, 4)):
                pool = string.digits if rng.random() < 0.5 else string.ascii_letters
                parts.append("".join(rng.choice(pool) for _ in range(rng.randint(1, 7))))
                parts.append(rng.choice(["", "", "-", "_", "."]))
            code = "".join(parts)[: barcode.ALNUM_MAX_LEN].strip("-_.")
        if barcode.detect_format(code) in (barcode.FORMAT_NUMERIC, barcode.FORMAT_ALNUM):
            codes.add(code)
    return sorted(codes)


def test_random_free_codes_encode_exactly_and_fit_with_wide_bars():
    width = labels.LABEL_CANVAS_SIZE[0]
    for code in _random_free_codes():
        modules = labels._code128_modules(code)
        assert _decode_modules(modules) == code, code
        assert labels._module_dots(len(modules), width) >= 2, code


def test_professional_item_label_has_logo_header_product_metadata_and_divider():
    data = labels.generate_item_label_png_bytes(PRODUCT)
    img = Image.open(io.BytesIO(data))
    scan = _scan_label(img)
    width, _ = img.size
    rows = scan["rows"]
    bars_top = scan["band_top"]
    header_top = labels._MARGIN_Y_PX
    header_bottom = header_top + labels._HEADER_HEIGHT_PX
    logo_right = labels._MARGIN_X_PX + labels._LOGO_MAX_WIDTH_PX
    text_left = logo_right + labels._HEADER_TEXT_GAP_PX

    assert scan["text"] == PRODUCT["id"]
    assert labels._load_brand_logo() is not None
    assert _region_ink(rows, labels._MARGIN_X_PX, header_top, logo_right, header_bottom) > 50
    assert _region_ink(rows, text_left, header_top, width - labels._MARGIN_X_PX, header_bottom) > 50
    assert sum(rows[header_bottom]) >= width - 2 * labels._MARGIN_X_PX - 2
    assert _region_ink(
        rows, labels._MARGIN_X_PX, header_bottom + labels._DIVIDER_PX,
        width - labels._MARGIN_X_PX, bars_top,
    ) > 50


def test_brand_logo_asset_is_versioned_and_converts_to_monochrome():
    assert labels.LABEL_LOGO_PATH.is_file()
    logo = labels._load_brand_logo()
    assert logo is not None
    assert logo.mode == "L"
    assert set(logo.getdata()) <= {0, 255}
    assert 0 in logo.getdata()


def test_thermal_typography_uses_legible_sizes_weight_and_tracking():
    assert labels._INSTITUTION_FONT_PX >= 14
    assert labels._TYPE_FONT_PX >= 12
    assert labels._NOTICE_FONT_PX >= 10
    assert labels._NAME_FONT_PX >= 18
    assert labels._META_FONT_PX >= 11
    assert labels._CODE_FONT_PX >= 26
    assert labels._MIN_FONT_PX >= 9
    assert labels._HEADER_TRACKING_PX >= 1

def test_label_without_notice_or_description_only_has_bars_and_code():
    img = labels.generate_label_image("LAB-MIC-01", notice=None)
    scan = _scan_label(img)
    assert scan["text"] == "LAB-MIC-01"
    assert _text_lines(scan["rows"], 0, scan["band_top"]) == 0
    assert _text_lines(scan["rows"], scan["band_top"] + scan["band_height"], img.size[1]) == 1


def test_generate_label_png_bytes_returns_valid_png():
    data = labels.generate_label_png_bytes("1-2-05-12-001")
    assert data[:8] == b"\x89PNG\r\n\x1a\n"

    img = Image.open(io.BytesIO(data))
    assert img.size == labels.LABEL_CANVAS_SIZE


def test_label_png_for_lab_mic_01_declares_printer_dpi_and_scans_back():
    data = labels.generate_label_png_bytes("LAB-MIC-01", description="Microscopio óptico")
    img = Image.open(io.BytesIO(data))
    assert img.info["dpi"] == pytest.approx((203, 203), abs=0.5)
    assert _scan_label(img)["text"] == "LAB-MIC-01"


def test_print_ready_pdf_has_exact_50x25mm_page_and_native_203dpi_raster():
    data = labels.generate_label_pdf_bytes(
        PRODUCT["id"], description=PRODUCT["name"], category=PRODUCT["category"],
        location=PRODUCT["location"], item_type=PRODUCT["item_type"],
    )
    assert data.startswith(b"%PDF-1.4")
    assert data.endswith(b"%%EOF\n")
    assert b"/Width 384 /Height 192" in data
    assert b"/BitsPerComponent 1" in data

    media_box = re.search(rb"/MediaBox \[0 0 ([0-9.]+) ([0-9.]+)\]", data)
    assert media_box
    assert float(media_box.group(1)) * 25.4 / 72 == pytest.approx(50, abs=0.01)
    assert float(media_box.group(2)) * 25.4 / 72 == pytest.approx(25, abs=0.01)

    matrix = re.search(
        rb"q\n([0-9.]+) 0 0 ([0-9.]+) ([0-9.]+) ([0-9.]+) cm", data,
    )
    assert matrix
    assert float(matrix.group(1)) * labels.LABEL_DPI / 72 == pytest.approx(384, abs=0.02)
    assert float(matrix.group(2)) * labels.LABEL_DPI / 72 == pytest.approx(192, abs=0.02)
    assert float(matrix.group(3)) > 0
    assert float(matrix.group(4)) > 0

    startxref = int(re.search(rb"startxref\n(\d+)", data).group(1))
    assert data[startxref:].startswith(b"xref\n")


def test_generate_item_label_pdf_uses_item_and_skips_unprintable_ids():
    assert labels.generate_item_label_pdf_bytes(PRODUCT).startswith(b"%PDF-1.4")
    assert labels.generate_item_label_pdf_bytes({"id": "CAJA 001"}) is None
    assert labels.generate_item_label_pdf_bytes({"id": ""}) is None

def test_generate_item_label_png_bytes_uses_the_item_and_skips_unprintable_ids():
    data = labels.generate_item_label_png_bytes({"id": "0012345", "name": "Osciloscopio"})
    assert _scan_label(Image.open(io.BytesIO(data)))["text"] == "0012345"
    assert labels.generate_item_label_png_bytes({"id": "CAJA 001", "name": "Caja vieja"}) is None
    assert labels.generate_item_label_png_bytes({"id": "", "name": "Sin codigo"}) is None


def test_generate_item_label_forwards_professional_metadata(monkeypatch):
    captured = {}

    def fake_generate(**kwargs):
        captured.update(kwargs)
        return b"png"

    monkeypatch.setattr(labels, "generate_label_png_bytes", fake_generate)
    assert labels.generate_item_label_png_bytes(PRODUCT) == b"png"
    assert captured == {
        "code": "LAB-MIC-01",
        "description": "Microscopio óptico binocular",
        "category": "Óptica",
        "location": "Estantería 2 · Piso 1",
        "item_type": "standalone",
    }

def test_container_level_code_with_na_zeros_generates_and_scans_exactly():
    """El codigo que originaba el error tambien debe producir una etiqueta
    Code 128 cuyo contenido recuperado sea exactamente el mismo."""
    code = "2-1-01-00-000"
    img = labels.generate_label_image(code, description="Contenedor 1")
    assert _scan_label(img)["text"] == code


def test_label_canvas_matches_sat_tt460_spec():
    # 50x25mm @ 203dpi (aprox, redondeado a multiplos de 32px)
    assert labels.LABEL_DPI == 203
    assert labels.LABEL_WIDTH_MM == 50
    assert labels.LABEL_HEIGHT_MM == 25
    assert labels.LABEL_CANVAS_SIZE == (384, 192)
