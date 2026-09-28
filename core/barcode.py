# -*- coding: utf-8 -*-
"""
core/barcode.py - Validacion, interpretacion y resolucion de codigos de
inventario (ver core/labels.py para la generacion visual de etiquetas y los
nombres de tipo de item que ve el usuario).

Formatos aceptados para items NUEVOS, cada uno resuelto por regex.

Nomenclatura del laboratorio (Canvas de Estructura y Codificacion GLIOPS V3):

1. Estandar de 5 niveles, 100% numerico:
   [ESTANTERIA]-[PISO]-[CONTENEDOR]-[CAJA]-[ITEM]
   Solo digitos separados por guion. Estanteria: 1 a 3. Piso: 1 a 6.
   Contenedor/Caja/Item: enteros positivos, con o sin ceros a la izquierda.
   Ejemplo: 1-2-05-12-001

2. Mesas de trabajo (equipos de alto valor), alfanumerico:
   M[1|2]-E[n]
   Ejemplo: M1-E2

3. Exhibicion Lego, alfanumerico:
   E3-LM[n]
   Ejemplo: E3-LM07

Codigos libres, para equipos que ya traen su propio codigo o laboratorios que
no usan GLIOPS:

4. Numerico libre: solo digitos 0-9, hasta NUMERIC_MAX_LEN. Los ceros a la
   izquierda son parte del codigo: el id siempre se guarda como texto.
   Ejemplo: 0012345

5. Alfanumerico libre: letras A-Z/a-z sin tildes ni ñ, digitos y los
   separadores - _ . entre ellos (sin espacios, sin separadores repetidos ni
   al inicio o al final), con al menos una letra y hasta ALNUM_MAX_LEN
   caracteres. Se respeta exactamente lo escrito, mayusculas y minusculas
   incluidas.
   Ejemplo: LAB-MIC-01

Los formatos son mutuamente excluyentes. Un codigo con la FORMA de uno de
GLIOPS (solo digitos y guiones, o que empieza por M<n>-E o E<n>-LM) que no
cumple sus reglas se rechaza en vez de aceptarse como codigo libre, para que
un error de digitacion (4-2-05-12-001, M3-E1, m1-e2, E4-LM01) no cree un item
fuera de la nomenclatura. Todos los caracteres permitidos existen en Code 128
y los limites de longitud garantizan que el codigo de barras quepa legible en
la etiqueta de 50x25 mm (ver core/labels.py).
"""

import logging
import re

logger = logging.getLogger(__name__)

FORMAT_STANDARD = "estandar_numerico"
FORMAT_MESA = "mesa_trabajo"
FORMAT_LEGO = "exhibicion_lego"
FORMAT_NUMERIC = "numerico_libre"
FORMAT_ALNUM = "alfanumerico_libre"

ESTANTERIA_MIN, ESTANTERIA_MAX = 1, 3
PISO_MIN, PISO_MAX = 1, 6

# Con estos limites el Code 128 de un codigo libre tiene como maximo 13
# simbolos de datos, que caben en la etiqueta de 50x25mm @ 203dpi con modulos
# de 2 puntos (0,25 mm), el ancho minimo recomendado para lectores USB.
NUMERIC_MAX_LEN = 20
ALNUM_MAX_LEN = 13

_SEPARATORS = "-_."

# re.ASCII: \d solo acepta 0-9 (no digitos Unicode como "１", que Code 128 no
# puede codificar).
_STANDARD_RE = re.compile(r"^(\d+)-(\d+)-(\d+)-(\d+)-(\d+)$", re.ASCII)
_MESA_RE = re.compile(r"^M([12])-E(\d+)$", re.ASCII)
_LEGO_RE = re.compile(r"^E3-LM(\d+)$", re.ASCII)
_NUMERIC_RE = re.compile(r"^\d+$", re.ASCII)
_ALNUM_RE = re.compile(r"^[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*$", re.ASCII)
_HAS_LETTER_RE = re.compile(r"[A-Za-z]", re.ASCII)

# "Forma" de los formatos GLIOPS (ver el docstring del modulo).
_STANDARD_SHAPE_RE = re.compile(r"^[\d-]*-[\d-]*$", re.ASCII)
_MESA_SHAPE_RE = re.compile(r"^M\d+-?E", re.ASCII | re.IGNORECASE)
_LEGO_SHAPE_RE = re.compile(r"^E\d+-?LM", re.ASCII | re.IGNORECASE)

FORMAT_HELP = (
    "Formatos aceptados: "
    "[ESTANTERIA]-[PISO]-[CONTENEDOR]-[CAJA]-[ITEM] 100% numerico "
    "(ej. 1-2-05-12-001) · "
    "M1-E[n] o M2-E[n] para mesas de trabajo (ej. M1-E2) · "
    "E3-LM[n] para exhibicion Lego (ej. E3-LM07) · "
    f"numerico libre de hasta {NUMERIC_MAX_LEN} digitos (ej. 0012345) · "
    f"alfanumerico libre de hasta {ALNUM_MAX_LEN} caracteres con letras sin tildes, "
    "numeros y los separadores - _ . (ej. LAB-MIC-01)."
)


def parse_code(code: str) -> dict:
    """Identifica a cual de los formatos aceptados pertenece `code` (sin los
    espacios de alrededor) y lo descompone en sus componentes; los codigos
    libres solo devuelven su formato, porque no codifican ubicacion. Lanza
    ValueError con un mensaje explicativo (incluye los formatos validos) si no
    coincide con ninguno o si algun componente esta fuera de rango."""
    code = (code or "").strip()

    m = _STANDARD_RE.match(code)
    if m:
        estanteria, piso, contenedor, caja, item = (int(g) for g in m.groups())
        if not (ESTANTERIA_MIN <= estanteria <= ESTANTERIA_MAX):
            raise ValueError(
                f"Estanteria invalida en '{code}': debe estar entre "
                f"{ESTANTERIA_MIN} y {ESTANTERIA_MAX}."
            )
        if not (PISO_MIN <= piso <= PISO_MAX):
            raise ValueError(
                f"Piso invalido en '{code}': debe estar entre {PISO_MIN} y {PISO_MAX}."
            )
        if contenedor < 1 or caja < 1 or item < 1:
            raise ValueError(f"Contenedor, caja e item deben ser numeros positivos en '{code}'.")
        return {
            "format": FORMAT_STANDARD,
            "estanteria": estanteria,
            "piso": piso,
            "contenedor": contenedor,
            "caja": caja,
            "item": item,
        }

    m = _MESA_RE.match(code)
    if m:
        mesa, equipo = int(m.group(1)), int(m.group(2))
        if equipo < 1:
            raise ValueError(f"Numero de equipo invalido en '{code}'.")
        return {"format": FORMAT_MESA, "mesa": mesa, "equipo": equipo}

    m = _LEGO_RE.match(code)
    if m:
        modelo = int(m.group(1))
        if modelo < 1:
            raise ValueError(f"Numero de modelo invalido en '{code}'.")
        return {"format": FORMAT_LEGO, "estanteria": 3, "modelo": modelo}

    # Tiene la forma de un formato GLIOPS pero no cumple sus reglas.
    if _STANDARD_SHAPE_RE.match(code):
        raise ValueError(
            f"'{code}' parece un codigo estandar pero no tiene los 5 niveles numericos "
            f"[ESTANTERIA]-[PISO]-[CONTENEDOR]-[CAJA]-[ITEM] (ej. 1-2-05-12-001)."
        )
    if _MESA_SHAPE_RE.match(code):
        raise ValueError(
            f"'{code}' usa el prefijo reservado de las mesas de trabajo: debe ser "
            f"M1-E[n] o M2-E[n], en mayusculas (ej. M1-E2)."
        )
    if _LEGO_SHAPE_RE.match(code):
        raise ValueError(
            f"'{code}' usa el prefijo reservado de la exhibicion Lego: debe ser "
            f"E3-LM[n], en mayusculas (ej. E3-LM07)."
        )

    if _NUMERIC_RE.match(code):
        if len(code) > NUMERIC_MAX_LEN:
            raise ValueError(
                f"El codigo numerico '{code}' tiene {len(code)} digitos; el maximo es "
                f"{NUMERIC_MAX_LEN} para que el codigo de barras quepa legible en la etiqueta."
            )
        return {"format": FORMAT_NUMERIC}

    if _ALNUM_RE.match(code) and _HAS_LETTER_RE.search(code):
        if len(code) > ALNUM_MAX_LEN:
            raise ValueError(
                f"El codigo '{code}' tiene {len(code)} caracteres; el maximo para un codigo "
                f"alfanumerico es {ALNUM_MAX_LEN} para que el codigo de barras quepa legible "
                f"en la etiqueta."
            )
        return {"format": FORMAT_ALNUM}

    raise ValueError(_invalid_code_message(code))


def _invalid_code_message(code: str) -> str:
    """Mensaje para un codigo que no cumple ningun formato. Nombra los
    caracteres no permitidos, si los hay, porque es el error mas comun al
    escribir a mano (espacios, tildes, ñ)."""
    forbidden = sorted({ch for ch in code if not (ch.isascii() and (ch.isalnum() or ch in _SEPARATORS))})
    if forbidden:
        shown = ", ".join(dict.fromkeys("espacio" if ch.isspace() else f"'{ch}'" for ch in forbidden))
        return f"'{code}' contiene caracteres no permitidos ({shown}). {FORMAT_HELP}"
    return f"'{code}' no coincide con ningun formato de codigo valido. {FORMAT_HELP}"


def detect_format(code: str):
    """Version silenciosa de parse_code: devuelve el nombre del formato o
    None si `code` no es valido, sin lanzar excepcion."""
    try:
        return parse_code(code)["format"]
    except ValueError:
        return None


def is_valid_code(code: str) -> bool:
    return detect_format(code) is not None


def validate_code_format(code: str) -> str:
    """Valida `code` contra los formatos aceptados y devuelve el nombre del
    formato detectado. Lanza ValueError (con los formatos validos en el
    mensaje) si no coincide con ninguno. Usado por core/storage.py al dar de
    alta items nuevos y por core/labels.py antes de generar una etiqueta."""
    return parse_code(code)["format"]


def describe_parsed(parsed: dict) -> str:
    """Texto legible en español de los componentes ya interpretados de un
    codigo (ver parse_code), para mostrar en la UI de escaneo."""
    if not parsed:
        return ""
    fmt = parsed.get("format")
    if fmt == FORMAT_STANDARD:
        return (
            f"Estantería {parsed['estanteria']} · Piso {parsed['piso']} · "
            f"Contenedor {parsed['contenedor']:02d} · Caja {parsed['caja']:02d} · "
            f"Ítem {parsed['item']:03d}"
        )
    if fmt == FORMAT_MESA:
        return f"Mesa de trabajo {parsed['mesa']} · Equipo {parsed['equipo']}"
    if fmt == FORMAT_LEGO:
        return f"Estantería 3 (Exhibición Lego) · Modelo {parsed['modelo']}"
    if fmt == FORMAT_NUMERIC:
        return "Código numérico libre (sin ubicación codificada)"
    if fmt == FORMAT_ALNUM:
        return "Código alfanumérico libre (sin ubicación codificada)"
    return ""


def scan(storage, code: str) -> dict:
    code = (code or "").strip()
    if not code:
        return {"status": "error", "message": "El codigo de barras no puede estar vacio."}

    parsed = None
    try:
        parsed = parse_code(code)
    except ValueError:
        parsed = None  # codigo con nomenclatura anterior/libre: se sigue permitiendo buscar por id

    try:
        item = storage.get_item(code)
        if not item:
            return {"status": "not_found", "barcode": code, "parsed": parsed}

        if item.get("status") == "retired":
            return {"status": "error", "message": f"El item '{item.get('name')}' fue dado de baja."}

        if item.get("item_type") == "master":
            children = storage.get_children(code)
            for child in children:
                child["available"] = storage.get_available_quantity(child["id"])
            return {"status": "found_master", "item": item, "children": children, "parsed": parsed}

        item["available"] = storage.get_available_quantity(code)
        parent = None
        if item.get("parent_id"):
            parent = storage.get_item(item["parent_id"])
        return {"status": "found_item", "item": item, "parent": parent, "parsed": parsed}

    except Exception as e:
        logger.error(f"Error al escanear el codigo '{code}': {e}")
        return {"status": "error", "message": str(e)}
