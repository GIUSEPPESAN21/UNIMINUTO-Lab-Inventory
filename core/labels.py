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
# Generacion visual de etiquetas imprimibles (codigo de barras + texto)
# ---------------------------------------------------------------------------
# Etiqueta de 50x25mm pensada para imprimirse tal cual en una termica
# SAT TT 460 (u otra compatible) a 203 dpi. De arriba hacia abajo:
#   1. mensaje institucional / advertencia (opcional, LABEL_NOTICE_TEXT)
#   2. descripcion corta del item (opcional, una linea: si no cabe se reduce
#      la fuente y al final se recorta con "...")
#   3. codigo de barras Code 128
#   4. el codigo completo en texto legible (nunca se recorta)
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

from barcode.charsets import code128 as _code128_tables
from PIL import Image, ImageDraw, ImageFont

from core.barcode import is_valid_code, validate_code_format

LABEL_DPI = 203  # resolucion nativa de la SAT TT 460
LABEL_WIDTH_MM = 50
LABEL_HEIGHT_MM = 25
# Aproximacion en pixeles de 50x25mm a 203dpi (399x200 exacto), redondeada a
# multiplos de 32px como espera el firmware de impresoras termicas tipo SAT.
LABEL_CANVAS_SIZE = (384, 192)

# Mensaje institucional / advertencia de la linea superior. Para omitirlo,
# pasa notice=None (o "") a generate_label_image / generate_label_png_bytes.
LABEL_NOTICE_TEXT = "Propiedad de UNIMINUTO · No retirar sin préstamo"

# Geometria para el lienzo por defecto, en puntos de impresora (= pixeles);
# textos y margenes escalan si se pide otro canvas_size.
_MIN_QUIET_MODULES = 6    # zona de silencio minima a cada lado de las barras
_MAX_MODULE_DOTS = 4      # modulo mas ancho: 4 puntos = 0,5 mm
_MAX_BAR_HEIGHT_MM = 12.0
_MIN_BAR_HEIGHT_PX = 24   # por debajo se omiten el aviso y luego la descripcion
_MARGIN_X_PX = 8          # ~1 mm a cada lado de los textos
_MARGIN_Y_PX = 4
_GAP_PX = 4
_NOTICE_FONT_PX = 14
_DESC_FONT_PX = 18
_DESC_MIN_FONT_PX = 14
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

# Valores de simbolo Code 128 (ISO/IEC 15417) que usa el codificador.
_START_B, _START_C = 104, 105
_TO_B_FROM_C, _TO_C_FROM_B = 100, 99
_STOP_PATTERN = _code128_tables.STOP + "11"  # parada completa: 13 modulos


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


def _code128_values(code: str) -> list:
    """Valores de simbolo Code 128 de `code`: inicio, datos y checksum.

    Usa el subconjunto B (todo el ASCII imprimible) y el C (2 digitos por
    simbolo) en tramos de 4 o mas digitos, o si el codigo son exactamente 2
    digitos; en un tramo impar el primer digito va en B. Asi los simbolos de
    datos nunca superan la longitud del codigo, que es en lo que se apoyan
    los limites de longitud de core/barcode.py.

    Se implementa aqui, en vez de usar el codificador de python-barcode
    0.16.1, porque ese pierde un "99" inicial (su optimizacion del simbolo de
    inicio confunde el par de digitos 99 con el cambio al subconjunto C) y la
    etiqueta debe devolver EXACTAMENTE el codigo registrado. De python-barcode
    solo se usa la tabla estandar de patrones de barras."""
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
    """Patron de modulos ('1' barra, '0' espacio) del Code 128 de `code`, de la
    barra de inicio a la de parada, sin zonas de silencio."""
    return "".join(_code128_tables.CODES[v] for v in _code128_values(code)) + _STOP_PATTERN


def _module_dots(n_modules: int, width_px: int) -> int:
    """Mayor numero entero de puntos por modulo (hasta _MAX_MODULE_DOTS) con el
    que las barras y sus zonas de silencio minimas caben en `width_px`; 0 si
    no caben ni con 1 punto."""
    for dots in range(_MAX_MODULE_DOTS, 0, -1):
        if (n_modules + 2 * _MIN_QUIET_MODULES) * dots <= width_px:
            return dots
    return 0


def _clean_text(value) -> str:
    """Texto en una sola linea; lo que no sea str (None, NaN de pandas) queda vacio."""
    return " ".join(value.split()) if isinstance(value, str) else ""


def _fit_font(text: str, max_width: int, size: int, min_size: int, bold: bool):
    """Fuente mas grande, de `size` hacia abajo hasta `min_size`, con la que
    `text` cabe en `max_width` (si ni asi cabe, la de `min_size`)."""
    size = max(size, min_size)
    font = _load_font(size, bold)
    while size > min_size and font.getlength(text) > max_width:
        size -= 1
        font = _load_font(size, bold)
    return font


def _ellipsize(text: str, font, max_width: int) -> str:
    """Recorta `text` con "..." al final hasta que quepa en `max_width`."""
    if font.getlength(text) <= max_width:
        return text
    while text and font.getlength(text.rstrip() + "...") > max_width:
        text = text[:-1]
    return text.rstrip() + "..." if text else ""


def generate_label_image(code: str, canvas_size: tuple = LABEL_CANVAS_SIZE, *,
                         description: str = None, notice: str = LABEL_NOTICE_TEXT) -> Image.Image:
    """Genera la etiqueta imprimible completa sobre un lienzo en blanco y negro
    de `canvas_size` px (por defecto 50x25mm @ 203dpi): el mensaje
    institucional `notice` y la descripcion corta `description` arriba (ambos
    opcionales), el codigo de barras Code 128 de `code` centrado y el codigo
    completo en texto legible debajo.

    Lanza ValueError si `code` no cumple ninguno de los formatos validos (ver
    core/barcode.validate_code_format) o si sus barras no caben en el ancho
    del lienzo."""
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

    text_width = max(1, width - 2 * px(_MARGIN_X_PX))
    margin_y, gap = px(_MARGIN_Y_PX), px(_GAP_PX)

    # Filas sobre las barras, en orden: aviso institucional y descripcion.
    top_rows = []
    notice = _clean_text(notice)
    if notice:
        font = _fit_font(notice, text_width, px(_NOTICE_FONT_PX), _MIN_FONT_PX, bold=False)
        top_rows.append((_ellipsize(notice, font, text_width), font))
    description = _clean_text(description)
    if description:
        min_size = max(_MIN_FONT_PX, px(_DESC_MIN_FONT_PX))
        font = _fit_font(description, text_width, px(_DESC_FONT_PX), min_size, bold=True)
        top_rows.append((_ellipsize(description, font, text_width), font))
    top_rows = [(text, font) for text, font in top_rows if text]

    # El codigo legible nunca se recorta: solo se reduce la fuente.
    code_font = _fit_font(code, text_width, px(_CODE_FONT_PX), _MIN_FONT_PX, bold=True)

    def ink_height(text: str, font) -> int:
        _, top, _, bottom = font.getbbox(text)
        return bottom - top

    def room_for_bars(rows) -> int:
        used = sum(ink_height(text, font) for text, font in rows) + ink_height(code, code_font)
        return height - 2 * margin_y - used - (len(rows) + 1) * gap

    # En lienzos muy bajos se sacrifican las lineas opcionales (primero el aviso).
    while top_rows and room_for_bars(top_rows) < px(_MIN_BAR_HEIGHT_PX):
        top_rows.pop(0)
    room = room_for_bars(top_rows)
    bar_h = max(1, min(px(_MAX_BAR_HEIGHT_MM * LABEL_DPI / 25.4), room))

    canvas = Image.new("L", (width, height), color=255)
    draw = ImageDraw.Draw(canvas)

    def draw_centered(text: str, font, top_y: int) -> int:
        left, top, right, bottom = font.getbbox(text)
        draw.text(((width - (right - left)) // 2 - left, top_y - top), text, font=font, fill=0)
        return bottom - top

    # Todo el bloque (textos + barras) se centra verticalmente.
    y = margin_y + max(0, room - bar_h) // 2
    for text, font in top_rows:
        y += draw_centered(text, font, y) + gap

    bar_x = (width - len(modules) * dots) // 2
    for run in re.finditer("1+", modules):
        draw.rectangle(
            [bar_x + run.start() * dots, y, bar_x + run.end() * dots - 1, y + bar_h - 1], fill=0
        )
    y += bar_h + gap

    draw_centered(code, code_font, y)
    return canvas.convert("1", dither=Image.Dither.NONE)


def generate_label_png_bytes(code: str, canvas_size: tuple = LABEL_CANVAS_SIZE, *,
                             description: str = None, notice: str = LABEL_NOTICE_TEXT) -> bytes:
    """Como generate_label_image pero devuelve bytes PNG listos para
    st.download_button o para enviar directo a la impresora. El PNG declara
    203 dpi: impreso a tamano real (100 %, sin "ajustar a la pagina") cada
    pixel cae en un punto de la SAT TT 460 y las barras salen exactas."""
    img = generate_label_image(code, canvas_size=canvas_size, description=description, notice=notice)
    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=(LABEL_DPI, LABEL_DPI))
    return buf.getvalue()


def generate_item_label_png_bytes(item: dict):
    """Etiqueta de un item del inventario: su id como codigo de barras y su
    nombre como descripcion corta. Devuelve None si el id no es un codigo
    imprimible (ej. un id heredado fuera de todo formato), para que la vista
    simplemente no muestre el boton de descarga."""
    item = item or {}
    code = str(item.get("id") or "").strip()
    if not is_valid_code(code):
        return None
    try:
        return generate_label_png_bytes(code, description=item.get("name"))
    except ValueError:
        return None
