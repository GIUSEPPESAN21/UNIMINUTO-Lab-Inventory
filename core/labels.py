# -*- coding: utf-8 -*-
"""
core/labels.py - Nomenclatura unica para los tipos de item, usada por todas
las vistas para que el lenguaje sea consistente en toda la aplicacion.

Los valores internos ("master", "child", "standalone") se mantienen tal cual
en core/storage.py y en los CSV de importacion (son claves tecnicas, no
texto para el usuario). Este modulo es la UNICA fuente de los nombres que
ve el usuario, para poder evolucionarlos sin tocar la logica de negocio.

- Contenedor Principal ("master"): la caja, kit o gabinete fisico que agrupa
  productos relacionados.
- Contenedor de Caracteristica ("child"): una subdivision con nombre propio
  DENTRO de un Contenedor Principal, que agrupa unidades que comparten una
  caracteristica (ej. "Resistencias 220 ohm", "Tornillos M4 x 20mm").
- Item Individual ("standalone"): un producto con codigo propio que no
  pertenece a ningun Contenedor Principal.
"""

ITEM_TYPE_ICONS = {
    "master": "🗄️",
    "child": "🧩",
    "standalone": "🔹",
}

ITEM_TYPE_NAMES = {
    "master": "Contenedor Principal",
    "child": "Contenedor de Característica",
    "standalone": "Ítem Individual",
}

ITEM_TYPE_LABELS = {k: f"{ITEM_TYPE_ICONS[k]} {v}" for k, v in ITEM_TYPE_NAMES.items()}

# Opciones en el orden en que se muestran en los formularios de creacion.
ITEM_TYPE_CHOICES = [
    ITEM_TYPE_NAMES["standalone"],
    ITEM_TYPE_NAMES["master"],
    ITEM_TYPE_NAMES["child"],
]

ITEM_TYPE_BY_CHOICE = {
    ITEM_TYPE_NAMES["standalone"]: "standalone",
    ITEM_TYPE_NAMES["master"]: "master",
    ITEM_TYPE_NAMES["child"]: "child",
}

ITEM_TYPE_HELP = {
    "standalone": "Un producto con su propio codigo, sin contenedor (ej. un multimetro).",
    "master": "La caja, kit o gabinete fisico que va a agrupar productos (ej. 'Caja de Electronica').",
    "child": "Una subdivision DENTRO de un Contenedor Principal ya creado, para una caracteristica "
             "especifica (ej. 'Resistencias 220 ohm', 'Tornillos M4').",
}


def type_label(item_type: str) -> str:
    return ITEM_TYPE_LABELS.get(item_type, item_type)


def type_name(item_type: str) -> str:
    return ITEM_TYPE_NAMES.get(item_type, item_type)


# ---------------------------------------------------------------------------
# Generacion visual de etiquetas imprimibles (marca + producto + Code 128)
# ---------------------------------------------------------------------------
# Etiqueta de 50x25mm pensada para imprimirse tal cual en una termica
# SAT TT 460 (u otra compatible) a 203 dpi. La composicion profesional es:
# 1. logotipo institucional + laboratorio, tipo de activo y advertencia
# 2. nombre del producto y metadatos compactos (categoria / ubicacion)
# 3. codigo de barras Code 128
# 4. codigo completo en texto legible (nunca se recorta)
#
# Code 128 codifica todo el ASCII imprimible, asi que sirve tal cual para los
# codigos numericos y alfanumericos (ver core/barcode.py) y lo leen los
# lectores USB 1D que ya usa el laboratorio (un QR exigiria lectores 2D).
# Las barras se dibujan con un numero ENTERO de puntos de impresora por
# modulo y sin reescalar: a 203 dpi un modulo fraccionario produce barras de
# anchos desiguales que un lector puede no reconocer.

import functools
import io
import re
import zlib
from pathlib import Path

from barcode.charsets import code128 as _code128_tables
from PIL import Image, ImageDraw, ImageFont, ImageOps

from core.barcode import is_valid_code, validate_code_format


LABEL_DPI = 203  # resolucion nativa de la SAT TT 460
LABEL_WIDTH_MM = 50
LABEL_HEIGHT_MM = 25
# Aproximacion en pixeles de 50x25mm a 203dpi (399x200 exacto), redondeada a
# multiplos de 32px como espera el firmware de impresoras termicas tipo SAT.
LABEL_CANVAS_SIZE = (384, 192)
LABEL_LOGO_PATH = Path(__file__).resolve().parent.parent / "assets" / "uniminuto-logo.png"
LABEL_INSTITUTION_TEXT = "LABORATORIO DE INGENIERÍA"
# Para omitir todo el encabezado institucional, pasa notice=None (o "").
LABEL_NOTICE_TEXT = "ACTIVO INSTITUCIONAL · NO RETIRAR SIN PRÉSTAMO"

# Geometria para el lienzo por defecto, en puntos de impresora (= pixeles);
# textos y margenes escalan si se pide otro canvas_size.
_MIN_QUIET_MODULES = 6
_MAX_MODULE_DOTS = 4
_MIN_BAR_HEIGHT_MM = 9.0
_MAX_BAR_HEIGHT_MM = 10.0
_MARGIN_X_PX = 8
_MARGIN_Y_PX = 4
_GAP_PX = 2
_HEADER_HEIGHT_PX = 46
_DIVIDER_PX = 1
_LOGO_MAX_WIDTH_PX = 78
_LOGO_MAX_HEIGHT_PX = 42
_HEADER_TEXT_GAP_PX = 8
_HEADER_TRACKING_PX = 1
_INSTITUTION_FONT_PX = 14
_TYPE_FONT_PX = 12
_NOTICE_FONT_PX = 10
_NAME_FONT_PX = 18
_NAME_MIN_FONT_PX = 15
_META_FONT_PX = 11
_CODE_FONT_PX = 26
_MIN_FONT_PX = 9

_FONT_CANDIDATES_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
_FONT_CANDIDATES_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]

_START_B, _START_C = 104, 105
_TO_B_FROM_C, _TO_C_FROM_B = 100, 99
_STOP_PATTERN = _code128_tables.STOP + "11"


@functools.lru_cache(maxsize=64)
def _load_font(size: int, bold: bool = True):
    candidates = _FONT_CANDIDATES_BOLD if bold else _FONT_CANDIDATES_REGULAR + _FONT_CANDIDATES_BOLD
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


@functools.lru_cache(maxsize=1)
def _load_brand_logo():
    """Logo versionado, monocromatico y recortado; None activa fallback."""
    try:
        with Image.open(LABEL_LOGO_PATH) as source:
            rgba = source.convert("RGBA")
            white = Image.new("RGBA", rgba.size, "white")
            white.alpha_composite(rgba)
            gray = ImageOps.grayscale(white.convert("RGB"))
            mono = gray.point(lambda value: 0 if value < 248 else 255)
            bbox = ImageOps.invert(mono).getbbox()
            return mono.crop(bbox).copy() if bbox else None
    except (OSError, ValueError):
        return None


def _code128_values(code: str) -> list:
    """Valores Code 128: inicio, datos y checksum, preservando el texto."""
    if not code or any(not (32 <= ord(ch) <= 126) for ch in code):
        raise ValueError(f"Code 128 solo admite texto ASCII imprimible: {code!r}")
    values = []
    charset = None

    def use(target: str) -> None:
        nonlocal charset
        if charset == target:
            return
        if charset is None:
            values.append(_START_B if target == "B" else _START_C)
        else:
            values.append(_TO_B_FROM_C if target == "B" else _TO_C_FROM_B)
        charset = target

    i, n = 0, len(code)
    while i < n:
        j = i
        while j < n and code[j] in "0123456789":
            j += 1
        digits = j - i
        if digits >= 4 or (digits == 2 and i == 0 and j == n):
            if digits % 2:
                use("B")
                values.append(ord(code[i]) - 32)
                i += 1
            use("C")
            while i < j:
                values.append(int(code[i:i + 2]))
                i += 2
        else:
            use("B")
            values.append(ord(code[i]) - 32)
            i += 1
    checksum = values[0] + sum(pos * value for pos, value in enumerate(values[1:], start=1))
    return values + [checksum % 103]


def _code128_modules(code: str) -> str:
    return "".join(_code128_tables.CODES[v] for v in _code128_values(code)) + _STOP_PATTERN


def _module_dots(n_modules: int, width_px: int) -> int:
    for dots in range(_MAX_MODULE_DOTS, 0, -1):
        if (n_modules + 2 * _MIN_QUIET_MODULES) * dots <= width_px:
            return dots
    return 0


def _clean_text(value) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _text_width(text: str, font, tracking: int = 0) -> float:
    """Ancho visual incluyendo espaciado adicional entre caracteres."""
    return font.getlength(text) + max(0, len(text) - 1) * tracking


def _fit_font(text: str, max_width: int, size: int, min_size: int,
              bold: bool, tracking: int = 0):
    size = max(size, min_size)
    font = _load_font(size, bold)
    while size > min_size and _text_width(text, font, tracking) > max_width:
        size -= 1
        font = _load_font(size, bold)
    return font


def _ellipsize(text: str, font, max_width: int, tracking: int = 0) -> str:
    if _text_width(text, font, tracking) <= max_width:
        return text
    while text and _text_width(text.rstrip() + "...", font, tracking) > max_width:
        text = text[:-1]
    return text.rstrip() + "..." if text else ""


def _ink_height(text: str, font) -> int:
    _, top, _, bottom = font.getbbox(text)
    return max(1, bottom - top)


def _metadata_line(category=None, location=None) -> str:
    fields = []
    category = _clean_text(category)
    location = _clean_text(location)
    if category:
        fields.append(f"CATEGORÍA: {category}")
    if location:
        fields.append(f"UBICACIÓN: {location}")
    return "  ·  ".join(fields)


def _item_label_fields(item: dict) -> dict:
    item = item or {}
    return {
        "code": str(item.get("id") or "").strip(),
        "description": _clean_text(item.get("name")),
        "category": _clean_text(item.get("category")),
        "location": _clean_text(item.get("location")),
        "item_type": _clean_text(item.get("item_type")),
    }


def generate_label_image(code: str, canvas_size: tuple = LABEL_CANVAS_SIZE, *,
                         description: str = None, notice: str = LABEL_NOTICE_TEXT,
                         category: str = None, location: str = None,
                         item_type: str = None) -> Image.Image:
    """Etiqueta profesional 50x25mm: marca, producto, Code 128 y texto exacto.

    `description` conserva su nombre historico por compatibilidad y representa
    el nombre corto. `notice=None` mantiene la variante minima sin encabezado.
    """
    code = (code or "").strip()
    validate_code_format(code)
    width, height = canvas_size
    modules = _code128_modules(code)
    dots = _module_dots(len(modules), width)
    if not dots:
        raise ValueError(
            f"El codigo '{code}' es demasiado largo: su codigo de barras no cabe en "
            f"una etiqueta de {width}px de ancho."
        )
    scale = min(width / LABEL_CANVAS_SIZE[0], height / LABEL_CANVAS_SIZE[1])

    def px(value: float) -> int:
        return max(1, round(value * scale))

    margin_x, margin_y, gap = px(_MARGIN_X_PX), px(_MARGIN_Y_PX), px(_GAP_PX)
    text_width = max(1, width - 2 * margin_x)
    description = _clean_text(description)
    notice = _clean_text(notice)
    metadata = _metadata_line(category, location)
    friendly_type = type_name(_clean_text(item_type)) or "Activo de laboratorio"
    rows = []
    if description:
        name_font = _fit_font(
            description, text_width, px(_NAME_FONT_PX),
            max(_MIN_FONT_PX, px(_NAME_MIN_FONT_PX)), bold=True,
        )
        rows.append(("name", _ellipsize(description, name_font, text_width), name_font))
    if metadata:
        meta_font = _fit_font(metadata, text_width, px(_META_FONT_PX), _MIN_FONT_PX, bold=True)
        rows.append(("metadata", _ellipsize(metadata, meta_font, text_width), meta_font))
    code_font = _fit_font(code, text_width, px(_CODE_FONT_PX), _MIN_FONT_PX, bold=True)
    code_height = _ink_height(code, code_font)
    branded = bool(notice)
    header_height = px(_HEADER_HEIGHT_PX)
    divider = px(_DIVIDER_PX)
    min_bar_height = px(_MIN_BAR_HEIGHT_MM * LABEL_DPI / 25.4)
    max_bar_height = px(_MAX_BAR_HEIGHT_MM * LABEL_DPI / 25.4)

    def body_y(current_rows, with_branding: bool) -> int:
        y_value = margin_y
        if with_branding:
            y_value += header_height + divider + gap
        for _, text, font in current_rows:
            y_value += _ink_height(text, font) + gap
        return y_value

    def bar_room(current_rows, with_branding: bool) -> int:
        return height - margin_y - code_height - gap - body_y(current_rows, with_branding)

    while rows and bar_room(rows, branded) < min_bar_height:
        metadata_index = next((i for i, row in enumerate(rows) if row[0] == "metadata"), None)
        rows.pop(metadata_index if metadata_index is not None else -1)
    if branded and bar_room(rows, branded) < min_bar_height:
        branded = False
    room = bar_room(rows, branded)
    if room < 1:
        raise ValueError("El lienzo es demasiado bajo para una etiqueta legible.")
    bar_height = min(max_bar_height, room)

    canvas = Image.new("L", (width, height), color=255)
    draw = ImageDraw.Draw(canvas)

    def draw_left(text: str, font, x: int, top_y: int, tracking: int = 0) -> int:
        left, top, _, bottom = font.getbbox(text)
        if not tracking:
            draw.text((x - left, top_y - top), text, font=font, fill=0)
        else:
            cursor = float(x)
            for char in text:
                char_left, _, _, _ = font.getbbox(char)
                draw.text((round(cursor) - char_left, top_y - top), char, font=font, fill=0)
                cursor += font.getlength(char) + tracking
        return bottom - top

    def draw_centered(text: str, font, top_y: int) -> int:
        left, top, right, bottom = font.getbbox(text)
        draw.text(((width - (right - left)) // 2 - left, top_y - top), text, font=font, fill=0)
        return bottom - top

    y = margin_y
    if branded:
        logo_width = px(_LOGO_MAX_WIDTH_PX)
        logo_height = min(header_height, px(_LOGO_MAX_HEIGHT_PX))
        logo = _load_brand_logo()
        if logo is not None:
            logo = logo.copy()
            logo.thumbnail((logo_width, logo_height), Image.Resampling.LANCZOS)
            logo = logo.point(lambda value: 0 if value < 192 else 255)
            logo_x = margin_x + max(0, (logo_width - logo.width) // 2)
            logo_y = y + max(0, (header_height - logo.height) // 2)
            canvas.paste(logo, (logo_x, logo_y))
        else:
            fallback = _fit_font("UNIMINUTO", logo_width, px(12), _MIN_FONT_PX, bold=True)
            draw_left("UNIMINUTO", fallback, margin_x, y + (header_height - _ink_height("UNIMINUTO", fallback)) // 2)

        header_x = margin_x + logo_width + px(_HEADER_TEXT_GAP_PX)
        header_width = max(1, width - margin_x - header_x)
        tracking = px(_HEADER_TRACKING_PX)
        institution_font = _fit_font(
            LABEL_INSTITUTION_TEXT, header_width, px(_INSTITUTION_FONT_PX),
            _MIN_FONT_PX, bold=True, tracking=tracking,
        )
        institution_text = _ellipsize(
            LABEL_INSTITUTION_TEXT, institution_font, header_width, tracking=tracking,
        )
        type_font = _fit_font(friendly_type, header_width, px(_TYPE_FONT_PX), _MIN_FONT_PX, bold=True)
        type_text = _ellipsize(friendly_type, type_font, header_width)
        notice_font = _fit_font(notice, header_width, px(_NOTICE_FONT_PX), _MIN_FONT_PX, bold=True)
        notice_text = _ellipsize(notice, notice_font, header_width)
        header_rows = [(institution_text, institution_font, tracking), (type_text, type_font, 0), (notice_text, notice_font, 0)]
        header_rows = [(text, font, spacing) for text, font, spacing in header_rows if text]
        header_gap = px(1)
        header_ink = sum(_ink_height(text, font) for text, font, _ in header_rows)
        header_ink += max(0, len(header_rows) - 1) * header_gap
        header_y = y + max(0, (header_height - header_ink) // 2)
        for text, font, spacing in header_rows:
            header_y += draw_left(text, font, header_x, header_y, spacing) + header_gap
        divider_y = y + header_height
        draw.line((margin_x, divider_y, width - margin_x - 1, divider_y), fill=0, width=divider)
        y += header_height + divider + gap

    for _, text, font in rows:
        y += draw_left(text, font, margin_x, y) + gap
    room = height - margin_y - code_height - gap - y
    y += max(0, (room - bar_height) // 2)
    bar_x = (width - len(modules) * dots) // 2
    for run in re.finditer("1+", modules):
        draw.rectangle(
            [bar_x + run.start() * dots, y, bar_x + run.end() * dots - 1, y + bar_height - 1],
            fill=0,
        )
    y += bar_height + gap
    draw_centered(code, code_font, y)
    return canvas.convert("1", dither=Image.Dither.NONE)


def generate_label_png_bytes(code: str, canvas_size: tuple = LABEL_CANVAS_SIZE, *,
                             description: str = None, notice: str = LABEL_NOTICE_TEXT,
                             category: str = None, location: str = None,
                             item_type: str = None) -> bytes:
    """Devuelve el PNG a 203dpi listo para imprimir al 100 %."""
    img = generate_label_image(
        code, canvas_size=canvas_size, description=description, notice=notice,
        category=category, location=location, item_type=item_type,
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=(LABEL_DPI, LABEL_DPI))
    return buf.getvalue()


def _assemble_pdf(objects: list[bytes]) -> bytes:
    """Construye un PDF minimo y valido sin dependencias adicionales."""
    document = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, payload in enumerate(objects, start=1):
        offsets.append(len(document))
        document.extend(f"{number} 0 obj\n".encode("ascii"))
        document.extend(payload)
        document.extend(b"\nendobj\n")
    xref = len(document)
    document.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    document.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        document.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    document.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    return bytes(document)


def _label_image_to_pdf(image: Image.Image) -> bytes:
    """Página exacta 50x25mm; cada pixel mantiene un punto a 203dpi."""
    image = image.convert("1", dither=Image.Dither.NONE)
    page_width = LABEL_WIDTH_MM * 72 / 25.4
    page_height = LABEL_HEIGHT_MM * 72 / 25.4
    image_width = image.width * 72 / LABEL_DPI
    image_height = image.height * 72 / LABEL_DPI
    offset_x = (page_width - image_width) / 2
    offset_y = (page_height - image_height) / 2
    pixels = zlib.compress(image.tobytes())
    commands = (
        f"q\n{image_width:.4f} 0 0 {image_height:.4f} "
        f"{offset_x:.4f} {offset_y:.4f} cm\n/Label Do\nQ\n"
    ).encode("ascii")
    image_object = (
        f"<< /Type /XObject /Subtype /Image /Width {image.width} "
        f"/Height {image.height} /ColorSpace /DeviceGray /BitsPerComponent 1 "
        f"/Filter /FlateDecode /Length {len(pixels)} >>\nstream\n"
    ).encode("ascii") + pixels + b"\nendstream"
    content_object = (
        f"<< /Length {len(commands)} >>\nstream\n".encode("ascii")
        + commands + b"endstream"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_width:.4f} {page_height:.4f}] "
            f"/Resources << /XObject << /Label 4 0 R >> >> /Contents 5 0 R >>"
        ).encode("ascii"),
        image_object,
        content_object,
    ]
    return _assemble_pdf(objects)


def generate_label_pdf_bytes(code: str, canvas_size: tuple = LABEL_CANVAS_SIZE, *,
                             description: str = None, notice: str = LABEL_NOTICE_TEXT,
                             category: str = None, location: str = None,
                             item_type: str = None) -> bytes:
    """PDF de una página 50x25mm recomendado para impresión sin escalado."""
    image = generate_label_image(
        code, canvas_size=canvas_size, description=description, notice=notice,
        category=category, location=location, item_type=item_type,
    )
    return _label_image_to_pdf(image)


def generate_item_label_png_bytes(item: dict):
    """Etiqueta PNG de inventario; None para ids no imprimibles."""
    fields = _item_label_fields(item)
    if not is_valid_code(fields["code"]):
        return None
    try:
        return generate_label_png_bytes(**fields)
    except ValueError:
        return None


def generate_item_label_pdf_bytes(item: dict):
    """Etiqueta PDF 50x25mm de inventario; None para ids no imprimibles."""
    fields = _item_label_fields(item)
    if not is_valid_code(fields["code"]):
        return None
    try:
        return generate_label_pdf_bytes(**fields)
    except ValueError:
        return None