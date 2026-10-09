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
# La etiqueta se compone para su TAMAÑO REAL (ancho x alto en mm, configurable
# por el laboratorio) y la resolucion REAL de la impresora (SAT TT460: 203
# dpi). Cada pixel del raster es un punto del cabezal y el PDF mide
# exactamente lo mismo que la etiqueta: impreso "al 100 %" no se reduce ni se
# amplia. Si la etiqueta sale pequeña, casi siempre el tamaño configurado (o el
# papel del driver) no coincide con el rollo real: ver la hoja de prueba.
#
# Diseño adaptable con piso de legibilidad: ningun texto se imprime por debajo
# de ~5 pt y el espacio entre bloques es proporcional al tamaño. Si todo no
# cabe a un tamaño legible se omite primero lo menos importante (categoria,
# tipo, aviso, ...) en vez de encoger y apretar las letras; la interfaz informa
# que se omitio. Las letras se rasterizan en monocromo con hinting: trazos
# uniformes y letras separadas, sin el "empaste" que produce suavizar y luego
# umbralizar a 1 bit.
#
# Code 128 codifica todo el ASCII imprimible, asi que sirve tal cual para los
# codigos numericos y alfanumericos (ver core/barcode.py) y lo leen los
# lectores USB 1D que ya usa el laboratorio (un QR exigiria lectores 2D).
# Las barras se dibujan con un numero ENTERO de puntos de impresora por
# modulo y sin reescalar: un modulo fraccionario produce barras de anchos
# desiguales que un lector puede no reconocer.

import functools
import io
import itertools
import json
import math
import re
import zlib
from dataclasses import dataclass
from pathlib import Path

from barcode.charsets import code128 as _code128_tables
from PIL import Image, ImageDraw, ImageFont, ImageOps

from core import barcode as barcode_rules
from core.barcode import is_valid_code, validate_code_format


LABEL_DPI = 203  # resolucion nativa de la SAT TT460
SUPPORTED_DPI = (203, 300)
LABEL_WIDTH_MM = 50
LABEL_HEIGHT_MM = 25
# La SAT TT460 acepta rollos de 20 a 112 mm de ancho; su cabezal de 4 pulgadas
# imprime hasta ~104 mm. El alto cubre desde etiquetas pequeñas hasta 4 x 6 in
# (152,4 mm).
MIN_LABEL_WIDTH_MM, MAX_LABEL_WIDTH_MM = 20, 104
MIN_LABEL_HEIGHT_MM, MAX_LABEL_HEIGHT_MM = 15, 160
LABEL_SPEC_SETTING_KEY = "label_spec"

# Tamaños de rollo habituales (ancho x alto en mm).
LABEL_SIZE_PRESETS = {
    "50x25": (50, 25),
    "50x30": (50, 30),
    "60x40": (60, 40),
    "100x50": (100, 50),
    "100x75": (100, 75),
    "100x100": (100, 100),
    "100x150": (100, 150),
}

# Contenido opcional que el laboratorio puede activar o desactivar. El nombre,
# el codigo de barras y el codigo legible siempre se imprimen.
LABEL_CONTENT_OPTIONS = {
    "brand": "Logo y «Laboratorio de Ingeniería»",
    "notice": "Aviso «No retirar sin préstamo»",
    "route": "Ruta física (estantería › piso › contenedor…)",
    "location": "Ubicación registrada",
    "type": "Tipo de activo",
    "category": "Categoría",
}

LABEL_LOGO_PATH = Path(__file__).resolve().parent.parent / "assets" / "uniminuto-logo.png"
LABEL_INSTITUTION_TEXT = "LABORATORIO DE INGENIERÍA"
# Para omitir todo el encabezado institucional, pasa notice=None (o "").
LABEL_NOTICE_TEXT = "ACTIVO INSTITUCIONAL · NO RETIRAR SIN PRÉSTAMO"
LABEL_NOTICE_SHORT_TEXT = "NO RETIRAR SIN PRÉSTAMO"

# Barras: modulo entre 0,25 mm (minimo recomendado para lectores USB) y
# 0,5 mm; zona de silencio de 10 modulos si cabe (ISO/IEC 15417), nunca menos
# de 6. Alto minimo 7 mm (la recomendacion general es >= 6,35 mm o el 15 % del
# ancho del simbolo) y, si sobra espacio, hasta el 36 % del alto de la etiqueta.
_MIN_QUIET_MODULES = 6
_PREFERRED_QUIET_MODULES = 10
_MAX_MODULE_DOTS = 4  # 0,5 mm a 203 dpi
_MIN_MODULE_MM = 0.25
_MAX_MODULE_MM = 0.5
_MIN_BAR_MM = 7.0
_BAR_HEIGHT_RATIO = 0.36
_MAX_BAR_MM = 18.0
_LOGO_MIN_MM = 3.6
_SHRINK_LIMIT = 0.72  # un texto largo se reduce como mucho al 72 % antes de partirse u omitirse
# En el PDF el raster empieza 0,01 puntos de impresora hacia adentro y termina
# 0,001 antes de su ultimo punto (ver _label_image_to_pdf). Ambos superan con
# holgura el error numerico de los visores y ninguno mueve el centro de un pixel.
_PDF_INSET_START_DOTS = 0.01
_PDF_INSET_END_DOTS = 0.001


@dataclass(frozen=True)
class _TextStyle:
    pt: float          # tamaño preferido en la etiqueta de referencia (50 x 25 mm)
    floor_pt: float    # piso absoluto de legibilidad: nunca se imprime mas pequeño
    tracking: float    # espacio extra entre letras, en fraccion del tamaño (em)
    word_spacing: float = 0.0
    shrink: float = _SHRINK_LIMIT  # cuanto puede reducirse para caber (0 = hasta el piso)


# Antes: aviso 3,5 pt, metadatos 3,9 pt, tipo 4,3 pt y 1 punto entre lineas.
_STYLES = {
    "lab": _TextStyle(6.6, 5.4, 0.07, 0.10, shrink=0.0),
    "notice": _TextStyle(5.8, 5.0, 0.05, 0.10, shrink=0.0),
    "name": _TextStyle(9.0, 7.0, 0.03, 0.08),
    "meta": _TextStyle(6.2, 5.2, 0.03, 0.10),
    "code": _TextStyle(9.5, 6.0, 0.06, 0.0),
}

# Importancia relativa del contenido: si no todo cabe, se conserva el conjunto
# de mayor valor que quepa a tamaño legible. La marca institucional pesa mas
# que todos los datos secundarios juntos: solo se omite si no hay alternativa.
_FEATURE_WEIGHTS = {
    "name": 100, "brand": 70, "name_wrap": 32, "route": 26,
    "location": 20, "notice": 14, "type": 8, "category": 6,
}
_META_ORDER = ("route", "location", "type", "category")
# Barra vertical: los datos libres (p. ej. "Estantería 2 · Piso 1") ya pueden traer "·".
_META_SEPARATOR = "  |  "

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


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _mm_to_dots(mm: float, dpi: int) -> float:
    return mm * dpi / 25.4


def _pt_to_px(pt: float, dpi: int) -> float:
    return pt * dpi / 72


def _format_mm(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def _format_inches(mm: float) -> str:
    return f"{round(mm / 25.4, 2):g}".replace(".", ",")


# ---------------------------------------------------------------------------
# Especificacion de la etiqueta (tamaño real, resolucion y contenido)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LabelSpec:
    """Tamaño fisico real de la etiqueta, resolucion de la impresora y
    contenido opcional a imprimir. Valida sus valores al construirse."""

    width_mm: float = LABEL_WIDTH_MM
    height_mm: float = LABEL_HEIGHT_MM
    dpi: int = LABEL_DPI
    content: tuple = tuple(LABEL_CONTENT_OPTIONS)

    def __post_init__(self):
        try:
            width = round(float(self.width_mm), 1)
            height = round(float(self.height_mm), 1)
            dpi = int(self.dpi)
        except (TypeError, ValueError):
            raise ValueError("El ancho, el alto y la resolución deben ser números.") from None
        if not MIN_LABEL_WIDTH_MM <= width <= MAX_LABEL_WIDTH_MM:
            raise ValueError(
                f"El ancho de la etiqueta debe estar entre {MIN_LABEL_WIDTH_MM} y "
                f"{MAX_LABEL_WIDTH_MM} mm (la SAT TT460 imprime hasta ~104 mm)."
            )
        if not MIN_LABEL_HEIGHT_MM <= height <= MAX_LABEL_HEIGHT_MM:
            raise ValueError(
                f"El alto de la etiqueta debe estar entre {MIN_LABEL_HEIGHT_MM} y "
                f"{MAX_LABEL_HEIGHT_MM} mm."
            )
        if dpi not in SUPPORTED_DPI:
            raise ValueError("La resolución debe ser 203 dpi (SAT TT460) o 300 dpi.")
        content = self.content
        if isinstance(content, str):
            content = (content,)
        wanted = set(content or ())
        object.__setattr__(self, "width_mm", width)
        object.__setattr__(self, "height_mm", height)
        object.__setattr__(self, "dpi", dpi)
        object.__setattr__(self, "content", tuple(k for k in LABEL_CONTENT_OPTIONS if k in wanted))

    @property
    def canvas_size(self) -> tuple:
        """Puntos de impresora que caben en la etiqueta (sin pasarse del borde)."""
        return (
            int(_mm_to_dots(self.width_mm, self.dpi) + 1e-6),
            int(_mm_to_dots(self.height_mm, self.dpi) + 1e-6),
        )

    @property
    def size_text(self) -> str:
        return f"{_format_mm(self.width_mm)} × {_format_mm(self.height_mm)} mm"

    @property
    def inches_text(self) -> str:
        """La misma medida en pulgadas, como la muestran los drivers (4 × 3 in)."""
        return f"{_format_inches(self.width_mm)} × {_format_inches(self.height_mm)} in"

    def describe(self) -> str:
        return f"{self.size_text} · {self.dpi} dpi"

    @property
    def preset_key(self) -> str:
        for key, (width, height) in LABEL_SIZE_PRESETS.items():
            if (width, height) == (self.width_mm, self.height_mm):
                return key
        return "custom"

    def shows(self, key: str) -> bool:
        return key in self.content

    def to_dict(self) -> dict:
        return {
            "width_mm": self.width_mm, "height_mm": self.height_mm,
            "dpi": self.dpi, "content": list(self.content),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LabelSpec":
        """Construye desde datos guardados. Lanza ValueError si no son validos."""
        if not isinstance(data, dict):
            raise ValueError("Configuración de etiqueta inválida.")
        content = data.get("content", tuple(LABEL_CONTENT_OPTIONS))
        if not isinstance(content, (list, tuple)):
            raise ValueError("El contenido de la etiqueta debe ser una lista.")
        return cls(
            width_mm=data.get("width_mm", LABEL_WIDTH_MM),
            height_mm=data.get("height_mm", LABEL_HEIGHT_MM),
            dpi=data.get("dpi", LABEL_DPI),
            content=tuple(content),
        )


LABEL_CANVAS_SIZE = LabelSpec().canvas_size


def load_label_spec(storage) -> LabelSpec:
    """Configuracion de etiqueta guardada por el laboratorio, o la de fabrica
    (50 x 25 mm, 203 dpi) si no hay ninguna o el dato esta dañado: imprimir una
    etiqueta nunca debe fallar por la configuracion."""
    getter = getattr(storage, "get_setting", None)
    if getter is None:
        return LabelSpec()
    try:
        raw = getter(LABEL_SPEC_SETTING_KEY)
        return LabelSpec.from_dict(json.loads(raw)) if raw else LabelSpec()
    except Exception:
        return LabelSpec()


def save_label_spec(storage, spec: LabelSpec, actor_email: str = "") -> None:
    storage.set_setting(LABEL_SPEC_SETTING_KEY, json.dumps(spec.to_dict()), actor_email=actor_email)


# ---------------------------------------------------------------------------
# Fuentes, logo y medicion de texto
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=256)
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


@functools.lru_cache(maxsize=16384)
def _advance(char: str, size: int) -> float:
    return _load_font(size).getlength(char, mode="1")


@functools.lru_cache(maxsize=65536)
def _measure(text: str, size: int, tracking_em: float, word_spacing_em: float) -> float:
    """Ancho de un texto en la fuente de la etiqueta (cacheado: el diseño
    adaptable prueba muchas combinaciones con los mismos textos)."""
    return (
        sum(_advance(char, size) for char in text)
        + max(0, len(text) - 1) * tracking_em * size
        + text.count(" ") * word_spacing_em * size
    )


def _text_width(text: str, font, tracking: float = 0, word_spacing: float = 0) -> float:
    """Ancho visual con espaciado independiente de letras y palabras. Suma los
    avances letra por letra, igual que se dibuja (el espaciado no admite kerning)."""
    return (
        sum(font.getlength(char, mode="1") for char in text)
        + max(0, len(text) - 1) * tracking
        + text.count(" ") * word_spacing
    )


@functools.lru_cache(maxsize=4096)
def _ink_extent(text: str, size: int) -> tuple:
    """(arriba, abajo) de la tinta del texto respecto al origen de dibujo."""
    _, top, _, bottom = _load_font(size).getbbox(text, mode="1")
    return top, bottom


def _ink_height(text: str, font) -> int:
    _, top, _, bottom = font.getbbox(text, mode="1")
    return max(1, bottom - top)


def _clean_text(value) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _ellipsize(text: str, font, max_width: float, tracking: float = 0,
               word_spacing: float = 0) -> str:
    if _text_width(text, font, tracking, word_spacing) <= max_width:
        return text
    while text and _text_width(text.rstrip() + "…", font, tracking, word_spacing) > max_width:
        text = text[:-1]
    return text.rstrip() + "…" if text.strip() else ""


# ---------------------------------------------------------------------------
# Code 128
# ---------------------------------------------------------------------------

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


def _module_dots(n_modules: int, width_px: int, max_dots: int = _MAX_MODULE_DOTS,
                 quiet_modules: int = _MIN_QUIET_MODULES) -> int:
    for dots in range(max_dots, 0, -1):
        if (n_modules + 2 * quiet_modules) * dots <= width_px:
            return dots
    return 0


def _barcode_geometry(n_modules: int, width_px: int, dpi: int) -> tuple:
    """(puntos por modulo, modulos de zona de silencio). Modulo de hasta
    0,5 mm; la zona de silencio sube a 10 modulos cuando cabe sin achicarlo."""
    max_dots = max(1, round(_mm_to_dots(_MAX_MODULE_MM, dpi)))
    dots = _module_dots(n_modules, width_px, max_dots, _MIN_QUIET_MODULES)
    if not dots:
        return 0, _MIN_QUIET_MODULES
    quiet = _PREFERRED_QUIET_MODULES
    if (n_modules + 2 * quiet) * dots > width_px:
        quiet = _MIN_QUIET_MODULES
    return dots, quiet


# ---------------------------------------------------------------------------
# Diseño adaptable
# ---------------------------------------------------------------------------

def _location_guide(code: str) -> str:
    """Ruta fisica compacta derivada del codigo, sin cambiar el Code 128."""
    try:
        parsed = barcode_rules.parse_code(code)
    except ValueError:
        return ""
    fmt = parsed.get("format")
    if fmt == barcode_rules.FORMAT_STANDARD:
        parts = [
            f"E{parsed['estanteria']}",
            f"P{parsed['piso']}",
            f"C{parsed['contenedor']:02d}",
        ]
        if parsed["caja"]:
            parts.append(f"CJ{parsed['caja']:02d}")
        if parsed["item"]:
            parts.append(f"I{parsed['item']:03d}")
        return "RUTA: " + " › ".join(parts)
    if fmt == barcode_rules.FORMAT_MESA:
        return f"RUTA: MESA {parsed['mesa']} › EQ{parsed['equipo']}"
    if fmt == barcode_rules.FORMAT_LEGO:
        return f"RUTA: E3 › LEGO {parsed['modelo']:02d}"
    return ""


@dataclass
class LabelLine:
    """Una linea de texto ya ubicada: `x`/`top` son el borde izquierdo y la
    parte superior de su tinta, en puntos de impresora."""

    role: str
    text: str
    size: int
    tracking: float = 0.0
    word_spacing: float = 0.0
    x: int = 0
    top: int = 0
    inverse: bool = False

    @property
    def font(self):
        return _load_font(self.size)

    @property
    def ink_height(self) -> int:
        top, bottom = _ink_extent(self.text, self.size)
        return max(1, bottom - top)

    @property
    def width(self) -> float:
        style = _STYLES[self.role]
        return _measure(self.text, self.size, style.tracking, style.word_spacing)

    @property
    def box(self) -> tuple:
        return (self.x, self.top, self.x + math.ceil(self.width), self.top + self.ink_height)

    def pt(self, dpi: int) -> float:
        return self.size * 72 / dpi


@dataclass
class LabelLayout:
    """Etiqueta ya compuesta: todo lo necesario para dibujarla y para explicar
    en la interfaz que se mostro, que se omitio y por que."""

    spec: LabelSpec
    size: tuple
    lines: list
    logo_box: tuple
    divider_box: tuple
    band_box: tuple
    bar_box: tuple
    modules: str
    module_dots: int
    quiet_modules: int
    shown: tuple
    omitted: tuple
    shortened: tuple
    scale: float

    @property
    def module_mm(self) -> float:
        return self.module_dots * 25.4 / self.spec.dpi

    @property
    def bar_height_mm(self) -> float:
        return (self.bar_box[3] - self.bar_box[1]) * 25.4 / self.spec.dpi

    @property
    def min_text_pt(self) -> float:
        return min(line.pt(self.spec.dpi) for line in self.lines)

    def lines_for(self, role: str) -> list:
        return [line for line in self.lines if line.role == role]

    @property
    def warnings(self) -> list:
        notes = []
        if self.module_mm < _MIN_MODULE_MM - 0.01:
            notes.append(
                "Las barras quedan muy finas para este ancho: usa una etiqueta más ancha "
                "o un código más corto para que el lector las lea sin esfuerzo."
            )
        return notes


class _NoFit(Exception):
    """El conjunto de contenido probado no cabe a tamaño legible."""


def _type_scale(spec: LabelSpec) -> float:
    """Factor de tamaño del texto respecto a la etiqueta de referencia 50 x 25.
    Crece mas despacio que la etiqueta (exponente < 1): el espacio extra se usa
    para mostrar mas datos y dar aire, no solo para agrandar las letras."""
    return _clamp(min((spec.height_mm / 25) ** 0.55, 1.4 * (spec.width_mm / 50) ** 0.8), 0.8, 2.2)


def _style_sizes(role: str, dpi: int, scale: float) -> tuple:
    style = _STYLES[role]
    floor = math.ceil(_pt_to_px(style.floor_pt, dpi))
    preferred = max(floor, round(_pt_to_px(style.pt * scale, dpi)))
    low = max(floor, math.floor(preferred * style.shrink))
    return preferred, low


def _make_line(role: str, text: str, size: int, **extra) -> LabelLine:
    style = _STYLES[role]
    return LabelLine(role, text, size, style.tracking * size, style.word_spacing * size, **extra)


@functools.lru_cache(maxsize=65536)
def _fit_size(role: str, text: str, max_width: float, preferred: int, low: int):
    style = _STYLES[role]
    for size in range(preferred, low - 1, -1):
        if _measure(text, size, style.tracking, style.word_spacing) <= max_width:
            return size
    return None


def _fit(role: str, text: str, max_width: float, preferred: int, low: int):
    """La linea mas grande (entre preferred y low) que cabe en max_width, o None."""
    size = _fit_size(role, text, max_width, preferred, low)
    return None if size is None else _make_line(role, text, size)


def _fit_or_ellipsize(role: str, text: str, max_width: float, preferred: int, low: int):
    line = _fit(role, text, max_width, preferred, low)
    if line is not None:
        return line, False
    style = _STYLES[role]
    clipped = _ellipsize(text, _load_font(low), max_width, style.tracking * low, style.word_spacing * low)
    if not clipped:
        raise _NoFit(role)
    return _make_line(role, clipped, low), True


def _wrap_name(text: str, max_width: float, preferred: int, low: int) -> list:
    """Nombre en dos lineas equilibradas (sin recortar) o lista vacia."""
    words = text.split()
    if len(words) < 2:
        return []
    style = _STYLES["name"]
    for size in range(preferred, low - 1, -1):
        best = None
        for cut in range(1, len(words)):
            first, second = " ".join(words[:cut]), " ".join(words[cut:])
            widest = max(
                _measure(first, size, style.tracking, style.word_spacing),
                _measure(second, size, style.tracking, style.word_spacing),
            )
            if widest <= max_width and (best is None or widest < best[0]):
                best = (widest, first, second)
        if best:
            return [_make_line("name", best[1], size, inverse=True),
                    _make_line("name", best[2], size, inverse=True)]
    return []


def _label_fields(code: str, spec: LabelSpec, description, notice, category, location, item_type) -> dict:
    notice = _clean_text(notice)
    item_type = _clean_text(item_type)
    location = _clean_text(location)
    category = _clean_text(category)
    variants = []
    if notice:
        variants = [notice]
        if notice == LABEL_NOTICE_TEXT:
            variants.append(LABEL_NOTICE_SHORT_TEXT)
    meta = {
        "route": _location_guide(code) if spec.shows("route") else "",
        "location": f"UBIC: {location}" if location and spec.shows("location") else "",
        "type": ITEM_TYPE_NAMES.get(item_type, "") if spec.shows("type") else "",
        "category": f"CAT: {category}" if category and spec.shows("category") else "",
    }
    return {
        "code": code,
        "name": _clean_text(description),
        # notice=None (o "") conserva la variante minima sin encabezado.
        "brand": bool(notice) and spec.shows("brand"),
        "notice_variants": variants if spec.shows("notice") else [],
        "meta": meta,
    }


def _available_features(fields: dict) -> set:
    available = {key for key, text in fields["meta"].items() if text}
    if fields["name"]:
        available.update({"name", "name_wrap"})
    if fields["brand"]:
        available.add("brand")
    if fields["notice_variants"]:
        available.add("notice")
    return available


def _candidate_sets(available: set) -> list:
    keys = sorted(available)
    subsets = [frozenset(combo) for r in range(len(keys), -1, -1)
               for combo in itertools.combinations(keys, r)]
    subsets.sort(key=lambda s: (-sum(_FEATURE_WEIGHTS[k] for k in s), -len(s), sorted(s)))
    return subsets


def _compose(fields: dict, spec: LabelSpec, canvas: tuple, keep: frozenset, scale: float) -> LabelLayout:
    """Ubica el contenido `keep` a escala `scale`. Lanza _NoFit si no cabe."""
    width, height = canvas
    dpi = spec.dpi
    type_scale = _type_scale(spec) * scale

    def dots(mm: float) -> int:
        return max(1, round(_mm_to_dots(mm, dpi)))

    margin_x = dots(_clamp(0.03 * spec.width_mm, 1.5, 3.0))
    margin_y = dots(_clamp(0.06 * spec.height_mm, 1.5, 3.0))
    content_width = width - 2 * margin_x
    gap = max(3, round(_mm_to_dots(_clamp(0.032 * spec.height_mm, 0.75, 2.0), dpi) * scale))
    shortened = []

    def sizes(role: str) -> tuple:
        return _style_sizes(role, dpi, type_scale)

    # --- Encabezado: logo + laboratorio + (aviso | primer dato promovido) ---
    meta_items = [(key, fields["meta"][key]) for key in _META_ORDER if key in keep and fields["meta"][key]]
    header_lines, logo_size = [], None
    with_brand, with_notice = "brand" in keep, "notice" in keep
    if with_brand or with_notice:
        lab_pref, lab_low = sizes("lab")
        notice_pref, notice_low = sizes("notice")
        meta_pref, meta_low = sizes("meta")
        line_gap = max(2, round(0.28 * notice_pref))
        if with_brand:
            estimated = _ink_extent(LABEL_INSTITUTION_TEXT, lab_pref)
            text_block = estimated[1] - estimated[0]
            if with_notice or meta_items:
                text_block += line_gap + round(0.93 * (notice_pref if with_notice else meta_pref))
            logo_height = max(text_block, dots(_LOGO_MIN_MM))
            logo = _load_brand_logo()
            aspect = (logo.width / logo.height) if logo is not None else 2.2
            logo_size = (max(1, round(logo_height * aspect)), logo_height)
            text_x = margin_x + logo_size[0] + dots(1.2)
        else:
            text_x = margin_x
        text_width = width - margin_x - text_x
        if with_brand:
            lab = _fit("lab", LABEL_INSTITUTION_TEXT, text_width, lab_pref, lab_low)
            if lab is None:
                raise _NoFit("brand")
            header_lines.append(lab)
        if with_notice:
            notice_line = None
            for variant in fields["notice_variants"]:
                notice_line = _fit("notice", variant, text_width, notice_pref, notice_low)
                if notice_line is not None:
                    break
            if notice_line is None:
                raise _NoFit("notice")
            header_lines.append(notice_line)
        elif with_brand and meta_items:
            # Sin aviso, el primer dato (normalmente la ruta) sube junto al logo
            # y ahorra una fila completa.
            promoted = _fit("meta", meta_items[0][1], text_width, meta_pref, meta_low)
            if promoted is not None:
                header_lines.append(promoted)
                meta_items = meta_items[1:]
        for line in header_lines:
            line.x = text_x
        header_text_height = (
            sum(line.ink_height for line in header_lines) + line_gap * (len(header_lines) - 1)
        )
        header_height = max(header_text_height, logo_size[1] if logo_size else 0)
    else:
        line_gap = 0
        header_height = 0

    # --- Nombre en banda negra (1 o 2 lineas) ---
    name_lines, band_pad_y = [], 0
    if "name" in keep:
        name_pref, name_low = sizes("name")
        pad_x = max(2, round(0.42 * name_pref))
        band_pad_y = max(2, round(0.24 * name_pref))
        band_text_width = content_width - 2 * pad_x
        single = _fit("name", fields["name"], band_text_width, name_pref, name_low)
        if single is not None:
            name_lines = [single]
        elif "name_wrap" in keep:
            name_lines = _wrap_name(fields["name"], band_text_width, name_pref, name_low)
            if not name_lines:
                raise _NoFit("name_wrap")
        else:
            line, clipped = _fit_or_ellipsize("name", fields["name"], band_text_width, name_pref, name_low)
            name_lines = [line]
            if clipped:
                shortened.append("name")
        for line in name_lines:
            line.inverse = True
            line.x = margin_x + pad_x
        name_gap = max(2, round(0.22 * name_lines[0].size))
        band_height = (
            sum(line.ink_height for line in name_lines)
            + name_gap * (len(name_lines) - 1) + 2 * band_pad_y
        )
    else:
        band_height = 0
        name_gap = 0

    # --- Datos (ruta, ubicacion, tipo, categoria): 2 lineas como maximo en
    #     etiquetas bajas, hasta 4 en las altas ---
    meta_lines = []
    if meta_items:
        meta_pref, meta_low = sizes("meta")
        groups, current = [], []
        for key, text in meta_items:
            candidate = current + [text]
            if _fit("meta", _META_SEPARATOR.join(candidate), content_width, meta_pref, meta_low):
                current = candidate
                continue
            if current:
                groups.append(current)
                current = [text]
                if _fit("meta", text, content_width, meta_pref, meta_low):
                    continue
            if key == "route":
                raise _NoFit("route")  # la ruta nunca se recorta: o completa o nada
            groups.append([text])
            current = []
        if current:
            groups.append(current)
        if len(groups) > (2 if spec.height_mm < 40 else 4):
            raise _NoFit("meta")
        for group in groups:
            line, clipped = _fit_or_ellipsize(
                "meta", _META_SEPARATOR.join(group), content_width, meta_pref, meta_low,
            )
            if clipped:
                shortened.append("meta")
            meta_lines.append(line)
        common = min(line.size for line in meta_lines)
        meta_lines = [
            line if line.size == common else _make_line("meta", line.text, common)
            for line in meta_lines
        ]
        for line in meta_lines:
            line.x = margin_x
    meta_gap = max(2, round(0.3 * meta_lines[0].size)) if meta_lines else 0
    meta_height = sum(line.ink_height for line in meta_lines) + meta_gap * max(0, len(meta_lines) - 1)

    # --- Codigo de barras y codigo legible (siempre completos) ---
    modules = _code128_modules(fields["code"])
    module_dots, quiet = _barcode_geometry(len(modules), width, dpi)
    if not module_dots:
        raise ValueError(
            f"El código '{fields['code']}' es demasiado largo: su código de barras no cabe "
            f"en una etiqueta de {spec.size_text} ({width} puntos de ancho)."
        )
    code_pref, code_low = sizes("code")
    code_line = _fit("code", fields["code"], content_width, code_pref, code_low)
    if code_line is None:
        floor = math.ceil(_pt_to_px(_STYLES["code"].floor_pt, dpi))
        code_line = _fit("code", fields["code"], content_width, code_low, floor)
    if code_line is None:
        raise ValueError(
            f"El código '{fields['code']}' no cabe legible en una etiqueta de {spec.size_text}."
        )
    bar_min = dots(_MIN_BAR_MM)
    bar_preferred = max(bar_min, dots(_clamp(_BAR_HEIGHT_RATIO * spec.height_mm, _MIN_BAR_MM, _MAX_BAR_MM)))
    bar_code_gap = max(2, round(0.55 * gap))

    # --- Alto total, reparto del espacio sobrante y posiciones ---
    divider = max(2, dots(0.25)) if header_height else 0
    blocks = []  # (tipo, alto, separacion_previa)
    if header_height:
        blocks.append(("header", header_height, 0))
        blocks.append(("divider", divider, max(2, gap // 2)))
    if band_height:
        blocks.append(("band", band_height, gap if blocks else 0))
    if meta_lines:
        blocks.append(("meta", meta_height, gap if blocks else 0))
    blocks.append(("bars", bar_min, gap if blocks else 0))
    blocks.append(("code", code_line.ink_height, bar_code_gap))
    used = 2 * margin_y + sum(h + g for _, h, g in blocks)
    if used > height:
        raise _NoFit("height")

    spare = height - used
    bar_height = bar_min + min(spare, bar_preferred - bar_min)
    spare -= bar_height - bar_min
    growable = [i for i, (kind, _, g) in enumerate(blocks) if g and kind not in ("code", "divider")]
    if growable and spare > 0:
        extra = min(spare // len(growable), gap)
        blocks = [
            (kind, h, g + extra if i in growable else g) for i, (kind, h, g) in enumerate(blocks)
        ]
        spare -= extra * len(growable)

    y = margin_y + spare // 2
    logo_box = divider_box = band_box = bar_box = None
    for kind, block_height, block_gap in blocks:
        y += block_gap
        if kind == "header":
            if logo_size:
                logo_y = y + (block_height - logo_size[1]) // 2
                logo_box = (margin_x, logo_y, margin_x + logo_size[0], logo_y + logo_size[1])
            text_y = y + (block_height - header_text_height) // 2
            for line in header_lines:
                line.top = text_y
                text_y += line.ink_height + line_gap
        elif kind == "divider":
            divider_box = (margin_x, y, width - margin_x, y + block_height)
        elif kind == "band":
            band_box = (margin_x, y, width - margin_x, y + block_height)
            text_y = y + band_pad_y
            for line in name_lines:
                line.top = text_y
                text_y += line.ink_height + name_gap
        elif kind == "meta":
            text_y = y
            for line in meta_lines:
                line.top = text_y
                text_y += line.ink_height + meta_gap
        elif kind == "bars":
            block_height = bar_height
            bar_x = (width - len(modules) * module_dots) // 2
            bar_box = (bar_x, y, bar_x + len(modules) * module_dots, y + bar_height)
        else:
            code_line.x = (width - round(code_line.width)) // 2
            code_line.top = y
        y += block_height

    shown = tuple(k for k in ("brand", "notice", *_META_ORDER) if k in keep)
    return LabelLayout(
        spec=spec, size=canvas,
        lines=header_lines + name_lines + meta_lines + [code_line],
        logo_box=logo_box, divider_box=divider_box, band_box=band_box, bar_box=bar_box,
        modules=modules, module_dots=module_dots, quiet_modules=quiet,
        shown=shown, omitted=(), shortened=tuple(dict.fromkeys(shortened)), scale=scale,
    )


def layout_label(code: str, spec: LabelSpec = None, *, canvas_size: tuple = None,
                 description: str = None, notice: str = LABEL_NOTICE_TEXT,
                 category: str = None, location: str = None,
                 item_type: str = None) -> LabelLayout:
    """Compone la etiqueta: conserva el contenido mas valioso que cabe a tamaño
    legible y reporta lo omitido. Lanza ValueError si el codigo no es valido o
    no cabe en la etiqueta."""
    code = (code or "").strip()
    validate_code_format(code)
    spec = spec or LabelSpec()
    canvas = tuple(canvas_size) if canvas_size else spec.canvas_size
    fields = _label_fields(code, spec, description, notice, category, location, item_type)
    available = _available_features(fields)
    candidates = _candidate_sets(available)
    for scale in (1.0, 0.9, 0.8, 0.7):
        for keep in candidates:
            try:
                layout = _compose(fields, spec, canvas, keep, scale)
            except _NoFit:
                continue
            requested = {k for k in available if k in LABEL_CONTENT_OPTIONS or k == "name"}
            layout.omitted = tuple(
                k for k in ("name", *LABEL_CONTENT_OPTIONS) if k in requested and k not in keep
            )
            return layout
    raise ValueError(
        f"La etiqueta de {spec.size_text} es demasiado pequeña para imprimir el código "
        f"'{code}' de forma legible."
    )


def describe_omitted(layout: LabelLayout) -> list:
    """Nombres legibles del contenido que no cupo (para la interfaz)."""
    names = {"name": "Nombre", **LABEL_CONTENT_OPTIONS}
    return [names[key] for key in layout.omitted]


# ---------------------------------------------------------------------------
# Dibujo
# ---------------------------------------------------------------------------

def _draw_line(draw, line: LabelLine) -> None:
    font = line.font
    top, _ = _ink_extent(line.text, line.size)
    first_left = font.getbbox(line.text[:1], mode="1")[0] if line.text else 0
    pen = float(line.x - first_left)
    fill = 255 if line.inverse else 0
    for char in line.text:
        draw.text((round(pen), line.top - top), char, font=font, fill=fill)
        pen += font.getlength(char, mode="1") + line.tracking
        if char == " ":
            pen += line.word_spacing


def _paste_logo(canvas, box: tuple) -> None:
    x0, y0, x1, y1 = box
    logo = _load_brand_logo()
    if logo is None:
        draw = ImageDraw.Draw(canvas)
        draw.fontmode = "1"
        line = _fit("lab", "UNIMINUTO", x1 - x0, max(6, (y1 - y0) // 2), 6) or _make_line("lab", "UNIMINUTO", 6)
        line.x, line.top = x0, y0 + ((y1 - y0) - line.ink_height) // 2
        _draw_line(draw, line)
        return
    logo = logo.copy()
    logo.thumbnail((x1 - x0, y1 - y0), Image.Resampling.LANCZOS)
    logo = logo.point(lambda value: 0 if value < 160 else 255)
    canvas.paste(logo, (x0 + ((x1 - x0) - logo.width) // 2, y0 + ((y1 - y0) - logo.height) // 2))


def render_label(layout: LabelLayout) -> Image.Image:
    width, height = layout.size
    canvas = Image.new("L", (width, height), color=255)
    draw = ImageDraw.Draw(canvas)
    draw.fontmode = "1"  # glifos monocromos con hinting: nitidos a 203 dpi
    if layout.logo_box:
        _paste_logo(canvas, layout.logo_box)
    if layout.divider_box:
        x0, y0, x1, y1 = layout.divider_box
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=0)
    if layout.band_box:
        x0, y0, x1, y1 = layout.band_box
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=0)
    for line in layout.lines:
        _draw_line(draw, line)
    x0, y0, _, y1 = layout.bar_box
    for run in re.finditer("1+", layout.modules):
        draw.rectangle(
            (x0 + run.start() * layout.module_dots, y0,
             x0 + run.end() * layout.module_dots - 1, y1 - 1),
            fill=0,
        )
    return canvas.convert("1", dither=Image.Dither.NONE)


# ---------------------------------------------------------------------------
# Salidas: imagen, PNG, PDF y hoja de prueba
# ---------------------------------------------------------------------------

def _resolve_spec(spec: LabelSpec, canvas_size) -> LabelSpec:
    """Compatibilidad: un canvas_size explicito (en puntos a 203 dpi) define el
    tamaño fisico si no se paso una especificacion."""
    if spec is not None or not canvas_size:
        return spec or LabelSpec()
    width, height = canvas_size
    return LabelSpec(
        width_mm=_clamp(width * 25.4 / LABEL_DPI, MIN_LABEL_WIDTH_MM, MAX_LABEL_WIDTH_MM),
        height_mm=_clamp(height * 25.4 / LABEL_DPI, MIN_LABEL_HEIGHT_MM, MAX_LABEL_HEIGHT_MM),
    )


def generate_label_image(code: str, canvas_size: tuple = None, *,
                         description: str = None, notice: str = LABEL_NOTICE_TEXT,
                         category: str = None, location: str = None,
                         item_type: str = None, spec: LabelSpec = None) -> Image.Image:
    """Etiqueta profesional a tamaño real: marca, producto, Code 128 y texto exacto.

    `description` conserva su nombre historico por compatibilidad y representa
    el nombre corto. `notice=None` mantiene la variante minima sin encabezado.
    """
    spec = _resolve_spec(spec, canvas_size)
    layout = layout_label(
        code, spec, canvas_size=canvas_size, description=description, notice=notice,
        category=category, location=location, item_type=item_type,
    )
    return render_label(layout)


def generate_label_png_bytes(code: str, canvas_size: tuple = None, *,
                             description: str = None, notice: str = LABEL_NOTICE_TEXT,
                             category: str = None, location: str = None,
                             item_type: str = None, spec: LabelSpec = None) -> bytes:
    """PNG que declara la resolucion de la impresora (respaldo; el PDF es el
    formato recomendado porque los navegadores ignoran los dpi de un PNG)."""
    spec = _resolve_spec(spec, canvas_size)
    img = generate_label_image(
        code, canvas_size=canvas_size, description=description, notice=notice,
        category=category, location=location, item_type=item_type, spec=spec,
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=(spec.dpi, spec.dpi))
    return buf.getvalue()


def _pdf_text(value: str) -> str:
    """Cadena PDF en UTF-16BE (admite tildes y cualquier caracter)."""
    return "<" + ("﻿" + value).encode("utf-16-be").hex().upper() + ">"


def _assemble_pdf(objects: list, info_number: int = None) -> bytes:
    """Construye un PDF minimo y valido sin dependencias adicionales."""
    document = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
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
    info = f" /Info {info_number} 0 R" if info_number else ""
    document.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R{info} >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    return bytes(document)


def _label_image_to_pdf(image: Image.Image, spec: LabelSpec = None, title: str = "Etiqueta") -> bytes:
    """Pagina del tamaño exacto de la etiqueta; cada pixel ocupa un punto de
    la impresora. El catalogo pide al visor imprimir sin escalar (PrintScaling
    /None) y elegir el papel por el tamaño del PDF (PickTrayByPDFSize).

    El raster se ancla a la esquina SUPERIOR IZQUIERDA (no se centra) para que,
    al rasterizar a la resolucion de la impresora, cada pixel caiga sobre un
    punto entero del cabezal; el sobrante (< 1 punto) queda a la derecha y
    abajo. Ademas los bordes quedan apenas hacia adentro: los visores redondean
    los bordes de una imagen hacia afuera (PDFium: Chrome y Edge) o suman un
    pixel al borde final (poppler), asi que un borde en 399,0001 o en -0,00001
    les hacia pintar 400 columnas y remuestrear, con barras de ancho desigual.
    El margen final es minimo porque Chrome, con la escala Predeterminado,
    recorta el papel a puntos PDF enteros y trunca a pixeles: la etiqueta se
    reduce hasta un 0,6 % y el borde no debe cruzar al pixel anterior. Asi el
    raster se copia 1:1 (verificado con los motores de Chrome/Edge y de poppler
    a 203 y 300 dpi, en tests/test_label_print.py)."""
    spec = spec or LabelSpec()
    image = image.convert("1", dither=Image.Dither.NONE)
    dot = 72 / spec.dpi  # un punto de la impresora, en puntos PDF
    page_width = round(spec.width_mm * 72 / 25.4, 4)
    page_height = round(spec.height_mm * 72 / 25.4, 4)
    start, end = _PDF_INSET_START_DOTS, _PDF_INSET_END_DOTS
    image_width = (image.width - start - end) * dot
    image_height = (image.height - start - end) * dot
    offset_x = start * dot
    offset_y = page_height - (image.height - end) * dot
    pixels = zlib.compress(image.tobytes())
    commands = (
        f"q\n{image_width:.6f} 0 0 {image_height:.6f} "
        f"{offset_x:.6f} {offset_y:.6f} cm\n/Label Do\nQ\n"
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
        b"<< /Type /Catalog /Pages 2 0 R "
        b"/ViewerPreferences << /PrintScaling /None /PickTrayByPDFSize true >> >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_width:.4f} {page_height:.4f}] "
            f"/Resources << /XObject << /Label 4 0 R >> >> /Contents 5 0 R >>"
        ).encode("ascii"),
        image_object,
        content_object,
        (
            f"<< /Title {_pdf_text(f'{title} · {spec.describe()}')} "
            f"/Creator {_pdf_text('Inventario de Laboratorio UNIMINUTO')} >>"
        ).encode("ascii"),
    ]
    return _assemble_pdf(objects, info_number=6)


def generate_label_pdf_bytes(code: str, canvas_size: tuple = None, *,
                             description: str = None, notice: str = LABEL_NOTICE_TEXT,
                             category: str = None, location: str = None,
                             item_type: str = None, spec: LabelSpec = None) -> bytes:
    """PDF de una pagina del tamaño exacto de la etiqueta (formato recomendado)."""
    spec = _resolve_spec(spec, canvas_size)
    image = generate_label_image(
        code, canvas_size=canvas_size, description=description, notice=notice,
        category=category, location=location, item_type=item_type, spec=spec,
    )
    return _label_image_to_pdf(image, spec, title=f"Etiqueta {(code or '').strip()}")


# Rejillas de nitidez de la hoja de prueba: barras alternadas con el ancho de
# modulo del Code 128 (0,25-0,5 mm). Con la impresion punto por punto todas las
# barras de una rejilla salen iguales; si el visor o el driver remuestrea la
# imagen, unas salen mas gruesas que otras o grises.
_GRATING_MODULES_MM = (0.25, 0.35, 0.5)
_GRATING_BARS = 8
_GRATING_CAPTION = "Barras parejas = impresión exacta"


def generate_calibration_image(spec: LabelSpec = None) -> Image.Image:
    """Hoja de prueba del tamaño configurado: marco a 1 mm del borde, una regla
    milimetrada y tres rejillas de barras. Impresa y revisada revela si el
    driver reduce la pagina (regla mas corta), si el papel no coincide (marco
    cortado) o si la imagen se remuestrea (rejillas desiguales o grises)."""
    spec = spec or LabelSpec()
    width, height = spec.canvas_size
    dpi = spec.dpi
    scale = _type_scale(spec)

    def dots(mm: float) -> int:
        return round(_mm_to_dots(mm, dpi))

    canvas = Image.new("L", (width, height), color=255)
    draw = ImageDraw.Draw(canvas)
    draw.fontmode = "1"
    stroke = max(2, dots(0.25))
    inset = dots(1.0)
    draw.rectangle((inset, inset, width - 1 - inset, height - 1 - inset), outline=0, width=stroke)
    frame_w_mm = (width - 2 * inset) * 25.4 / dpi
    frame_h_mm = (height - 2 * inset) * 25.4 / dpi

    ruler_mm = max(10, int((spec.width_mm - 8) // 10) * 10)
    inner_width = width - 2 * (inset + stroke + dots(1.5))
    lines = []
    for role, text in (
        ("lab", "PRUEBA DE IMPRESIÓN"),
        ("meta", spec.describe()),
        ("meta", f"Regla {ruler_mm} mm · marco {_format_mm(round(frame_w_mm))} × "
                 f"{_format_mm(round(frame_h_mm))} mm"),
    ):
        preferred, low = _style_sizes(role, dpi, scale)
        line = _fit(role, text, inner_width, preferred, low)
        if line is not None:
            lines.append(line)
    ruler_height = dots(2.0) + stroke
    number_size = _style_sizes("meta", dpi, 1.0)[1]
    number_height = _ink_extent("0123456789", number_size)[1] - _ink_extent("0123456789", number_size)[0]
    ruler_block = ruler_height + dots(0.6) + number_height
    gap = dots(1.0)

    grating_modules = [max(1, dots(mm)) for mm in _GRATING_MODULES_MM]
    grating_widths = [(2 * _GRATING_BARS - 1) * module for module in grating_modules]
    grating_gap = dots(4.0)
    grating_width = sum(grating_widths) + grating_gap * (len(grating_widths) - 1)
    grating_height = dots(3.0)
    show_gratings = grating_width <= inner_width
    caption = None
    if show_gratings:
        preferred, low = _style_sizes("meta", dpi, scale)
        caption = _fit("meta", _GRATING_CAPTION, inner_width, preferred, low)

    available = height - 2 * (inset + stroke + dots(1.0))

    def total_height() -> int:
        total = ruler_block + sum(l.ink_height for l in lines) + gap * len(lines)
        if show_gratings:
            total += grating_height + gap
        if caption is not None:
            total += caption.ink_height + gap
        return total

    # Si no cabe todo, se sacrifica primero lo accesorio (leyenda, rejillas y
    # luego los textos), nunca la regla.
    while total_height() > available:
        if caption is not None:
            caption = None
        elif show_gratings:
            show_gratings = False
        elif lines:
            lines.pop()
        else:
            break
    y = (height - total_height()) // 2
    for line in lines[:2]:
        line.x = (width - round(line.width)) // 2
        line.top = y
        _draw_line(draw, line)
        y += line.ink_height + gap

    x_start = (width - dots(ruler_mm)) // 2
    baseline = y + ruler_height
    draw.rectangle((x_start, baseline - stroke, x_start + dots(ruler_mm), baseline - 1), fill=0)
    for mm in range(ruler_mm + 1):
        x = x_start + dots(mm)
        tick = dots(2.0) if mm % 10 == 0 else dots(1.3) if mm % 5 == 0 else dots(0.8)
        draw.rectangle((x, baseline - stroke - tick, x + max(1, stroke // 2), baseline - 1), fill=0)
        if mm % 10 == 0 or (ruler_mm < 20 and mm % 5 == 0):
            label = _make_line("meta", str(mm), number_size)
            label.x = x - round(label.width) // 2 + 1
            label.top = baseline + dots(0.6)
            _draw_line(draw, label)
    y = baseline + dots(0.6) + number_height + gap
    for line in lines[2:]:
        line.x = (width - round(line.width)) // 2
        line.top = y
        _draw_line(draw, line)
        y += line.ink_height + gap
    if show_gratings:
        x = (width - grating_width) // 2
        for module, grating in zip(grating_modules, grating_widths):
            for bar in range(_GRATING_BARS):
                left = x + 2 * bar * module
                draw.rectangle((left, y, left + module - 1, y + grating_height - 1), fill=0)
            x += grating + grating_gap
        y += grating_height + gap
    if caption is not None:
        caption.x = (width - round(caption.width)) // 2
        caption.top = y
        _draw_line(draw, caption)
    return canvas.convert("1", dither=Image.Dither.NONE)


def generate_calibration_pdf_bytes(spec: LabelSpec = None) -> bytes:
    spec = spec or LabelSpec()
    return _label_image_to_pdf(generate_calibration_image(spec), spec, title="Prueba de impresión")


# ---------------------------------------------------------------------------
# Etiquetas de items del inventario
# ---------------------------------------------------------------------------

def _item_label_fields(item: dict) -> dict:
    item = item or {}
    return {
        "code": str(item.get("id") or "").strip(),
        "description": _clean_text(item.get("name")),
        "category": _clean_text(item.get("category")),
        "location": _clean_text(item.get("location")),
        "item_type": _clean_text(item.get("item_type")),
    }


def generate_item_label_png_bytes(item: dict, spec: LabelSpec = None):
    """Etiqueta PNG de inventario; None para ids no imprimibles."""
    fields = _item_label_fields(item)
    if not is_valid_code(fields["code"]):
        return None
    try:
        return generate_label_png_bytes(**fields, spec=spec)
    except ValueError:
        return None


def generate_item_label_pdf_bytes(item: dict, spec: LabelSpec = None):
    """Etiqueta PDF de inventario a tamaño real; None para ids no imprimibles."""
    fields = _item_label_fields(item)
    if not is_valid_code(fields["code"]):
        return None
    try:
        return generate_label_pdf_bytes(**fields, spec=spec)
    except ValueError:
        return None


def layout_item_label(item: dict, spec: LabelSpec = None) -> LabelLayout:
    """Composicion de la etiqueta de un item (vista previa y diagnostico en la
    interfaz). Lanza ValueError si el codigo no es imprimible en ese tamaño."""
    fields = _item_label_fields(item)
    return layout_label(
        fields["code"], spec or LabelSpec(), description=fields["description"],
        category=fields["category"], location=fields["location"], item_type=fields["item_type"],
    )


@functools.lru_cache(maxsize=256)  # ~5-80 KB por etiqueta: acota la memoria
def _cached_item_label_files(spec: LabelSpec, code: str, description: str, category: str,
                             location: str, item_type: str) -> tuple:
    fields = dict(code=code, description=description, category=category,
                  location=location, item_type=item_type)
    image = generate_label_image(**fields, spec=spec)
    png = io.BytesIO()
    image.save(png, format="PNG", dpi=(spec.dpi, spec.dpi))
    return _label_image_to_pdf(image, spec, title=f"Etiqueta {code}"), png.getvalue()


def item_label_files(item: dict, spec: LabelSpec = None) -> tuple:
    """(pdf, png) de la etiqueta de un item, cacheados por contenido y tamaño:
    el catalogo no regenera todas las etiquetas en cada recarga. (None, None)
    si el codigo no es imprimible o no cabe en la etiqueta configurada."""
    fields = _item_label_fields(item)
    if not is_valid_code(fields["code"]):
        return None, None
    try:
        return _cached_item_label_files(spec or LabelSpec(), **fields)
    except ValueError:
        return None, None
