# -*- coding: utf-8 -*-
"""Pruebas de la nomenclatura de codigos (core/barcode.py): los 3 formatos
GLIOPS V3 (estandar numerico, mesas de trabajo, exhibicion Lego) con sus
reglas de rango, los codigos libres numericos y alfanumericos (con letras) y
el rechazo de cualquier otro string."""

import pytest

from core import barcode


@pytest.mark.parametrize(
    "code, expected",
    [
        ("1-2-05-12-001", {"format": "estandar_numerico", "estanteria": 1, "piso": 2, "contenedor": 5, "caja": 12, "item": 1}),
        ("3-6-1-1-1", {"format": "estandar_numerico", "estanteria": 3, "piso": 6, "contenedor": 1, "caja": 1, "item": 1}),
        ("M1-E2", {"format": "mesa_trabajo", "mesa": 1, "equipo": 2}),
        ("M2-E1", {"format": "mesa_trabajo", "mesa": 2, "equipo": 1}),
        ("E3-LM07", {"format": "exhibicion_lego", "estanteria": 3, "modelo": 7}),
        ("E3-LM1", {"format": "exhibicion_lego", "estanteria": 3, "modelo": 1}),
        # Codigos libres: numericos puros (con sus ceros a la izquierda)...
        ("0012345", {"format": "numerico_libre"}),
        ("7", {"format": "numerico_libre"}),
        ("12345678901234567890", {"format": "numerico_libre"}),  # 20 digitos, el maximo
        # ...y alfanumericos con letras.
        ("LAB-MIC-01", {"format": "alfanumerico_libre"}),
        ("lab-mic-01", {"format": "alfanumerico_libre"}),      # se respetan las minusculas
        ("CAJA-001", {"format": "alfanumerico_libre"}),        # nomenclatura anterior: ahora es un codigo libre
        ("SENSOR_T.02", {"format": "alfanumerico_libre"}),
        ("A", {"format": "alfanumerico_libre"}),
        ("M1", {"format": "alfanumerico_libre"}),              # no es el prefijo de mesas (M<n>-E)
        ("MESA-01", {"format": "alfanumerico_libre"}),
        ("99-LAB", {"format": "alfanumerico_libre"}),
        ("ABCDEFGHIJKLM", {"format": "alfanumerico_libre"}),   # 13 caracteres, el maximo
    ],
)
def test_parse_code_valid_formats(code, expected):
    assert barcode.parse_code(code) == expected


def test_parse_code_ignores_surrounding_whitespace():
    assert barcode.parse_code("  LAB-MIC-01\t") == {"format": "alfanumerico_libre"}
    assert barcode.parse_code(" 0012345 ") == {"format": "numerico_libre"}


@pytest.mark.parametrize(
    "code",
    [
        "4-2-05-12-001",   # estanteria fuera de rango (1 a 3)
        "0-2-05-12-001",   # estanteria fuera de rango
        "1-7-05-12-001",   # piso fuera de rango (1 a 6)
        "1-0-05-12-001",   # piso fuera de rango
        "1-2-00-12-001",   # contenedor debe ser >= 1
        "1-2-05-00-001",   # caja debe ser >= 1
        "1-2-05-12-000",   # item debe ser >= 1
    ],
)
def test_parse_code_standard_out_of_range(code):
    with pytest.raises(ValueError):
        barcode.parse_code(code)


@pytest.mark.parametrize(
    "code",
    [
        "M3-E1",       # solo se permite mesa 1 o 2
        "M0-E1",
        "m1-e2",       # case-sensitive: la nomenclatura oficial es en mayusculas
        "M1-EXTRA",    # prefijo reservado de las mesas de trabajo
        "E3-LM0",      # modelo debe ser >= 1
        "E3LM07",      # falta el guion
        "e3-lm07",     # prefijo reservado de la exhibicion Lego, en minusculas
        "E4-LM01",     # la exhibicion Lego vive solo en la estanteria 3
        "1-2-3",       # formato estandar incompleto (solo 3 niveles)
        "1.2.05.12.001",  # digitos con puntos: no es estandar ni tiene letras
        "12_34",       # sin letras: no es numerico puro ni alfanumerico
        "LAB MIC 01",  # espacios
        "LAB--MIC",    # separadores repetidos
        "-LAB01",      # separador al inicio
        "LAB01-",      # separador al final
        "LAB/MIC/01",  # barra no permitida
        "LAB-MÍC-01",  # tilde: Code 128 no la puede codificar
        "ÑANDU-01",
        "１２３４",     # digitos Unicode de ancho completo
        "123456789012345678901",  # 21 digitos (maximo 20)
        "ABCDEFGHIJKLMN",         # 14 caracteres (maximo 13)
        "",
        "   ",
    ],
)
def test_parse_code_rejects_invalid(code):
    with pytest.raises(ValueError):
        barcode.parse_code(code)


def test_parse_code_error_names_the_forbidden_characters():
    with pytest.raises(ValueError, match="espacio"):
        barcode.parse_code("LAB MIC 01")
    with pytest.raises(ValueError, match="Í"):
        barcode.parse_code("LAB-MÍC-01")


def test_parse_code_error_explains_reserved_prefixes_and_length_limits():
    with pytest.raises(ValueError, match="mesas de trabajo"):
        barcode.parse_code("m1-e2")
    with pytest.raises(ValueError, match="Lego"):
        barcode.parse_code("E3LM07")
    with pytest.raises(ValueError, match=str(barcode.ALNUM_MAX_LEN)):
        barcode.parse_code("ABCDEFGHIJKLMN")
    with pytest.raises(ValueError, match=str(barcode.NUMERIC_MAX_LEN)):
        barcode.parse_code("1" * 21)


def test_detect_format_and_is_valid_code():
    assert barcode.detect_format("1-2-05-12-001") == barcode.FORMAT_STANDARD
    assert barcode.detect_format("M1-E2") == barcode.FORMAT_MESA
    assert barcode.detect_format("E3-LM07") == barcode.FORMAT_LEGO
    assert barcode.detect_format("0012345") == barcode.FORMAT_NUMERIC
    assert barcode.detect_format("LAB-MIC-01") == barcode.FORMAT_ALNUM
    assert barcode.detect_format("no valido") is None

    assert barcode.is_valid_code("M1-E2") is True
    assert barcode.is_valid_code("LAB-MIC-01") is True
    assert barcode.is_valid_code("no valido") is False


def test_validate_code_format_returns_format_name():
    assert barcode.validate_code_format("E3-LM07") == barcode.FORMAT_LEGO
    assert barcode.validate_code_format("LAB-MIC-01") == barcode.FORMAT_ALNUM
    assert barcode.validate_code_format("0012345") == barcode.FORMAT_NUMERIC
    with pytest.raises(ValueError):
        barcode.validate_code_format("no valido")


def test_describe_parsed_for_each_format():
    std = barcode.parse_code("1-2-05-12-001")
    assert "Estantería 1" in barcode.describe_parsed(std)
    assert "Ítem 001" in barcode.describe_parsed(std)

    mesa = barcode.parse_code("M1-E2")
    assert "Mesa de trabajo 1" in barcode.describe_parsed(mesa)

    lego = barcode.parse_code("E3-LM07")
    assert "Modelo 7" in barcode.describe_parsed(lego)

    assert "numérico libre" in barcode.describe_parsed(barcode.parse_code("0012345"))
    assert "alfanumérico libre" in barcode.describe_parsed(barcode.parse_code("LAB-MIC-01"))


def test_scan_exposes_parsed_components_for_valid_format(storage):
    storage.add_item("M1-E1", name="Impresora 3D", item_type="standalone", quantity=1)
    result = barcode.scan(storage, "M1-E1")
    assert result["status"] == "found_item"
    assert result["parsed"] == {"format": "mesa_trabajo", "mesa": 1, "equipo": 1}


@pytest.mark.parametrize(
    "code, fmt", [("LAB-MIC-01", "alfanumerico_libre"), ("0012345", "numerico_libre")]
)
def test_scan_finds_free_codes_by_their_exact_text(storage, code, fmt):
    storage.add_item(code, name="Microscopio", item_type="standalone", quantity=2)
    result = barcode.scan(storage, code)
    assert result["status"] == "found_item"
    assert result["item"]["id"] == code
    assert result["parsed"] == {"format": fmt}


def test_scan_does_not_drop_leading_zeros(storage):
    storage.add_item("0012345", name="Osciloscopio", quantity=1)
    assert barcode.scan(storage, "12345")["status"] == "not_found"


def test_scan_still_works_for_legacy_ids_without_parsed_info(storage):
    """Los items ya cargados con una nomenclatura anterior que no cumple
    ningun formato deben seguir pudiendo escanearse; solo 'parsed' queda en None."""
    storage.add_item("CAJA 001", name="Caja vieja", item_type="master")
    result = barcode.scan(storage, "CAJA 001")
    assert result["status"] == "found_master"
    assert result["parsed"] is None


def test_scan_legacy_hyphenated_ids_now_parse_as_free_alphanumeric(storage):
    storage.add_item("CAJA-001", name="Caja vieja", item_type="master")
    result = barcode.scan(storage, "CAJA-001")
    assert result["status"] == "found_master"
    assert result["parsed"] == {"format": "alfanumerico_libre"}
