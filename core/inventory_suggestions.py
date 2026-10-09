# -*- coding: utf-8 -*-
"""
core/inventory_suggestions.py - Propone productos a partir de la descripcion
libre de un Contenedor Principal.

Ejemplo real: un contenedor con la descripcion

    "Contiene Piezas Lego de Pines 4x2 - 2x2 - 2x1
     Además, Contiene un Caja con Separadores de Fichas y Pieza lego Lisas de 2x2"

produce cinco Contenedores de Caracteristica ("child") listos para revisar:
"Pieza Lego con pines 4x2", "... 2x2", "... 2x1", "Caja con separadores de
fichas" y "Pieza Lego lisa 2x2", con codigo de nivel caja (2-1-01-NN-000),
la categoria del contenedor, una ubicacion coherente y cantidad 0 pendiente de
conteo (la descripcion no trae cantidades).

Todo el modulo son funciones PURAS (sin Streamlit ni storage): la vista
(views/inventario.py) muestra las propuestas para que el usuario las edite y
confirme, y core/storage.py vuelve a validar al guardar. Nada se inventa: lo
que el texto no deja claro (una medida "1x0", un termino generico como
"fichas", una pieza unida con "y" a una caja) se devuelve como NOTA para que
el usuario lo confirme.

Interpretacion (pensada para cualquier contenedor, no solo los de Lego):

1. El texto se divide en frases por saltos de linea, punto y coma y puntos
   (no los decimales), y cada frase en partes por "-", ",", ":", "y"/"e".
   Una parte unida con "y" o "," a la anterior solo se separa si empieza un
   producto nuevo (trae una medida, una cantidad, una caja o un sustantivo
   conocido): asi "Caja con puertas y ventanas" queda como un solo producto
   y "... Fichas y Pieza lego Lisas de 2x2" como dos.
2. Se descartan muletillas iniciales ("Además", "Contiene", "Hay", "un", ...).
3. Cada parte es una CAJA ("Caja con ..."), una PIEZA con una o varias medidas
   ("Pines 4x2", "Bases de 1 pin") o un PRODUCTO sin medida. Una medida suelta
   ("2x2") hereda el sujeto de la parte anterior ("Piezas Lego de Pines").
4. El nombre de una pieza se normaliza: sustantivo en singular + marca +
   caracteristica + medida ("Pieza Lego con pines 4x2", "Base Lego de 1 pin").
"""

import math
import re
import unicodedata

from core import barcode

DEFAULT_UNIT = "unidad"
PENDING_COUNT_NOTE = "Cantidad pendiente de conteo."
HISTORY_DETAILS = "Creado desde la descripción del contenedor {parent_id} (generador de productos)."

KIND_PIECE = "pieza"
KIND_BOX = "caja"
KIND_PRODUCT = "producto"

# Columnas de la vista previa editable (views/inventario.py).
COL_CREATE = "Crear"
COL_CODE = "Código"
COL_NAME = "Nombre"
COL_FEATURE = "Característica"
COL_QUANTITY = "Cantidad"
COL_UNIT = "Unidad"
COL_NOTES = "Notas"
EDITOR_COLUMNS = (COL_CREATE, COL_CODE, COL_NAME, COL_FEATURE, COL_QUANTITY, COL_UNIT, COL_NOTES)


# ---------------------------------------------------------------------------
# Vocabulario (claves sin tildes y en minuscula: ver fold())
# ---------------------------------------------------------------------------

# Sustantivo singular -> (como se muestra, genero gramatical).
_NOUNS = {
    "pieza": ("Pieza", "f"), "base": ("Base", "f"), "bloque": ("Bloque", "m"),
    "ladrillo": ("Ladrillo", "m"), "placa": ("Placa", "f"), "plancha": ("Plancha", "f"),
    "lamina": ("Lámina", "f"), "viga": ("Viga", "f"), "rueda": ("Rueda", "f"),
    "llanta": ("Llanta", "f"), "eje": ("Eje", "m"), "engranaje": ("Engranaje", "m"),
    "conector": ("Conector", "m"), "teja": ("Teja", "f"), "baldosa": ("Baldosa", "f"),
    "puerta": ("Puerta", "f"), "ventana": ("Ventana", "f"), "figura": ("Figura", "f"),
    "minifigura": ("Minifigura", "f"), "ficha": ("Ficha", "f"), "polea": ("Polea", "f"),
    "bisagra": ("Bisagra", "f"), "tornillo": ("Tornillo", "m"), "tuerca": ("Tuerca", "f"),
    "arandela": ("Arandela", "f"), "cable": ("Cable", "m"), "sensor": ("Sensor", "m"),
    "motor": ("Motor", "m"), "boton": ("Botón", "m"), "resistencia": ("Resistencia", "f"),
    "condensador": ("Condensador", "m"), "led": ("LED", "m"),
}

# Contenedores fisicos que pueden venir DENTRO de un contenedor.
_BOX_NOUNS = {
    "caja", "cajita", "bolsa", "estuche", "bandeja", "organizador", "recipiente",
    "tarro", "frasco", "compartimento", "compartimiento", "gaveta", "cajon",
}

# Caracteristica (singular) -> (clave canonica, texto femenino, texto masculino).
_CHARACTERISTICS = {
    "pin": ("pin", "con pines", "con pines"),
    "teton": ("pin", "con pines", "con pines"),
    "stud": ("pin", "con pines", "con pines"),
    "lisa": ("lisa", "lisa", "liso"),
    "liso": ("lisa", "lisa", "liso"),
    "bisel": ("bisel", "con bisel", "con bisel"),
    "biselada": ("bisel", "con bisel", "con bisel"),
    "biselado": ("bisel", "con bisel", "con bisel"),
    "inclinada": ("bisel", "con bisel", "con bisel"),
    "inclinado": ("bisel", "con bisel", "con bisel"),
    "plana": ("plana", "plana", "plano"),
    "plano": ("plana", "plana", "plano"),
    "curva": ("curva", "curva", "curvo"),
    "curvo": ("curva", "curva", "curvo"),
    "redonda": ("redonda", "redonda", "redondo"),
    "redondo": ("redonda", "redonda", "redondo"),
}

_BRANDS = {"lego": "Lego", "duplo": "Duplo", "technic": "Technic", "mindstorms": "Mindstorms"}
# Marcas que, nombradas en cualquier parte del contenedor, aplican a todas sus piezas
# (una linea como "Technic" solo se usa si la pieza la menciona).
_CONTEXT_BRANDS = {"lego", "duplo"}

_STOPWORDS = {
    "de", "del", "la", "las", "el", "los", "un", "una", "unos", "unas", "con", "y", "e",
    "o", "u", "para", "en", "tipo", "al", "a", "su", "sus", "que", "se", "por",
}

# Terminos que no dicen QUE es el producto: se piden confirmar.
_VAGUE = {
    "ficha", "cosa", "elemento", "accesorio", "material", "surtido", "surtida",
    "vario", "varia", "otro", "otra", "miscelaneo", "miscelanea", "repuesto", "objeto",
}

# Palabras que terminan en "s" sin ser plurales (y las que el singular
# aproximado no debe tocar).
_NOT_PLURAL = {
    "gris", "dos", "tres", "seis", "mas", "menos", "lunes", "chasis", "tesis", "crisis",
    "bus", "plus", "gas", "atlas", "lapiz", "x",
}
_IRREGULAR_SINGULAR = {"luces": "luz", "lapices": "lápiz", "peces": "pez", "veces": "vez", "cruces": "cruz"}
_VOWELS = set("aeiou")


def fold(text) -> str:
    """Minusculas y sin tildes (la ñ queda como n): para comparar palabras sin
    depender de como se escribieron."""
    decomposed = unicodedata.normalize("NFKD", "" if text is None else str(text))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def singular(word: str) -> str:
    """Singular aproximado (en minuscula) de un sustantivo o adjetivo en español.
    Suficiente para nombres de inventario: piezas->pieza, pines->pin,
    biseles->bisel, separadores->separador, bases->base, botones->botón."""
    low = (word or "").lower()
    folded = fold(low)
    if folded in _IRREGULAR_SINGULAR:
        return _IRREGULAR_SINGULAR[folded]
    if (len(folded) <= 3 or folded in _NOT_PLURAL or not folded.endswith("s")
            or folded.endswith(("is", "us")) or not folded[:-1].isalpha()):
        return low
    if folded.endswith("ones") and len(folded) > 5:
        return low[:-4] + "ón"
    if folded.endswith("es") and len(folded) > 4:
        stem = folded[:-2]
        if stem[-1] in "lnrd" and stem[-2] in _VOWELS:
            return low[:-2]
    return low[:-1]


def _word_key(word: str) -> str:
    return fold(singular(word))


# ---------------------------------------------------------------------------
# Expresiones
# ---------------------------------------------------------------------------

# Frases: salto de linea, punto y coma, viñetas y punto (no el decimal 2.5).
_STRONG_SPLIT = re.compile(r"\n+|;|[•·]|\.(?!\d)")
# Partes: coma, dos puntos, y/e, guiones con espacio y guion entre medidas (4x2-2x2).
_WEAK_SPLIT = re.compile(
    r"(\s*,\s*|\s*:\s*|\s+[ye]\s+|\s+[-–—]+\s*|\s*[-–—]+\s+|(?<=\d)\s*[-–—]\s*(?=\d))",
    re.IGNORECASE,
)
_DIM_RE = re.compile(r"(?<![\w.,])(\d{1,3})\s*[x×X*]\s*(\d{1,3})(?:\s*[x×X*]\s*(\d{1,3}))?(?!\w)")
_PIN_RE = re.compile(r"(?:\bde\s+)?(?<![\w.,])(\d{1,3})\s*(?:pin(?:es)?|tet[oó]n(?:es)?|studs?)\b", re.IGNORECASE)
_QTY_RE = re.compile(r"^(\d{1,6})\s+(?!(?:x|pin|pines|tet[oó]n|tetones|studs?)\b|[×*])(?=\S)", re.IGNORECASE)
_EDGE_JUNK = re.compile(r"^[\s\-–—*•·,:;()\"'«»]+|[\s\-–—*•·,:;.()\"'«»]+$")
_STARTERS = (
    r"adem[aá]s|tambi[eé]n|contienen?|incluyen?|tienen?|hay|traen?|llevan?|"
    r"se\s+encuentran?|encontramos|encuentras?"
)
_ARTICLES = r"y|e|un|una|unos|unas|el|la|los|las|algun[oa]s|vari[oa]s|otr[oa]s?"
_LEADING = re.compile(rf"^(?:(?P<starter>{_STARTERS})|(?:{_ARTICLES}))\b[\s,:]*", re.IGNORECASE)
_WORD_RE = re.compile(r"[^\W_]+")
_LETTER_RE = re.compile(r"[^\W\d_]")


def _collapse(text) -> str:
    return re.sub(r"\s+", " ", "" if text is None else str(text)).strip()


def _clean(raw: str):
    """Quita bordes y muletillas iniciales. Devuelve (texto, empezaba_con_verbo)."""
    text = _EDGE_JUNK.sub("", raw or "")
    starter = False
    while True:
        match = _LEADING.match(text)
        if not match or not match.group(0):
            break
        starter = starter or bool(match.group("starter"))
        text = _EDGE_JUNK.sub("", text[match.end():])
    return _collapse(text), starter


def _first_key(text: str) -> str:
    match = _LETTER_RE.search(text or "")
    if not match:
        return ""
    word = _WORD_RE.match(text, match.start())
    return _word_key(word.group(0)) if word else ""


def _is_box(text: str) -> bool:
    return _first_key(text) in _BOX_NOUNS


# ---------------------------------------------------------------------------
# Medidas
# ---------------------------------------------------------------------------

def _find_measures(text: str) -> list:
    """Medidas de la parte, en orden: dimensiones (4x2, 2 x 2 x 1) y numero de
    pines ("de 1 pin"). Cada una: {"text", "zero", "guess", "start", "end", "raw"}."""
    found = []
    for match in _DIM_RE.finditer(text):
        dims = [int(g) for g in match.groups() if g is not None]
        normalized = "x".join(str(d) for d in dims)
        zero = any(d == 0 for d in dims)
        guess = "x".join(str(d or 1) for d in dims) if zero else ""
        found.append({"text": normalized, "zero": zero, "guess": guess,
                      "start": match.start(), "end": match.end(), "raw": match.group(0)})
    taken = [(m["start"], m["end"]) for m in found]
    for match in _PIN_RE.finditer(text):
        if any(start < match.end() and match.start() < end for start, end in taken):
            continue
        count = int(match.group(1))
        normalized = f"de {count} pin" if count == 1 else f"de {count} pines"
        found.append({"text": normalized, "zero": count == 0, "guess": "de 1 pin" if count == 0 else "",
                      "start": match.start(), "end": match.end(), "raw": match.group(0)})
    return sorted(found, key=lambda m: m["start"])


def _canonical_dims(match) -> str:
    dims = sorted(int(g) for g in match.groups() if g is not None)
    return "x".join(str(d) for d in dims)


def product_key(name) -> frozenset:
    """Clave para detectar productos repetidos sin importar mayusculas, tildes,
    plurales, palabras vacias, marca ni el orden de la medida (4x2 == 2x4, como
    en las piezas Lego). "Pieza Lego con pines 4x2" == "Piezas de pines 2x4"."""
    text = _DIM_RE.sub(lambda m: f" {_canonical_dims(m)} ", fold(name))
    keys = set()
    for word in _WORD_RE.findall(text):
        if word in _STOPWORDS or word in _BRANDS:
            continue
        key = word if _DIM_RE.fullmatch(word) else _word_key(word)
        key = _CHARACTERISTICS.get(key, (key,))[0]
        if key in ("pieza", "", "con"):
            continue
        keys.add(key)
    return frozenset(keys)


def zero_measures(name) -> list:
    """Medidas con una dimension 0 dentro de un nombre ("1x0"): no existen."""
    return [m for m in _find_measures(str(name or "")) if m["zero"]]


# ---------------------------------------------------------------------------
# Partes de la descripcion
# ---------------------------------------------------------------------------

def _sep_kind(separator: str) -> str:
    token = (separator or "").strip().lower()
    if token == ",":
        return "comma"
    if token == ":":
        return "colon"
    if token in ("y", "e"):
        return "and"
    return "dash"


def _starts_new(clean: str, starter: bool, sep: str, prev_clean: str) -> bool:
    if starter or not clean or sep in ("strong", "dash", "colon"):
        return True
    if _find_measures(clean) or _QTY_RE.match(clean):
        return True
    first = _first_key(clean)
    if first in _BOX_NOUNS:
        return True
    if first in _NOUNS:
        # "Caja con puertas y ventanas": lo que sigue a una caja es su contenido.
        return not _is_box(prev_clean)
    return False


def _split_parts(text: str) -> list:
    """Partes interpretables, ya unidas cuando un "y"/"," no empieza un
    producto nuevo. Cada parte: {"raw", "clean", "sep", "after_box_and"}."""
    text = unicodedata.normalize("NFC", text or "").replace("\r\n", "\n").replace("\r", "\n")
    merged = []
    for clause in _STRONG_SPLIT.split(text):
        pieces = _WEAK_SPLIT.split(clause)
        for index in range(0, len(pieces), 2):
            raw = pieces[index]
            separator = pieces[index - 1] if index else ""
            sep = _sep_kind(separator) if index else "strong"
            clean, starter = _clean(raw)
            previous = merged[-1] if merged else None
            if previous and previous["clean"] and not _starts_new(clean, starter, sep, previous["clean"]):
                previous["raw"] = f"{previous['raw']}{separator}{raw}"
                previous["clean"] = _clean(previous["raw"])[0]
                continue
            if previous is not None and not previous["clean"]:
                merged.pop()  # solo muletillas ("Además,")
                previous = merged[-1] if merged else None
            merged.append({
                "raw": raw, "clean": clean, "sep": sep,
                "after_box_and": bool(sep == "and" and previous and _is_box(previous["clean"])),
                "prev_clean": previous["clean"] if previous else "",
            })
    return [part for part in merged if part["clean"]]


def _keep_as_written(word: str) -> bool:
    """Siglas cortas (USB, LED, EV3) y medidas se respetan tal cual."""
    return any(ch.isdigit() for ch in word) or (1 < len(word) <= 4 and word.isupper())


def _sentence_case(text: str) -> str:
    """"Caja con Puertas y Ventanas" -> "Caja con puertas y ventanas". Respeta
    marcas, siglas (EV3, USB) y medidas."""
    words = []
    for index, word in enumerate(text.split()):
        key = fold(word)
        if key in _BRANDS:
            words.append(_BRANDS[key])
        elif _keep_as_written(word):
            words.append(word)
        elif index == 0:
            words.append(word[:1].upper() + word[1:].lower())
        else:
            words.append(word.lower())
    return " ".join(words)


def _strip_stopword_edges(text: str) -> str:
    words = _collapse(_EDGE_JUNK.sub("", text or "")).split()
    while words and fold(words[0]) in _STOPWORDS:
        words.pop(0)
    while words and fold(words[-1]) in _STOPWORDS:
        words.pop()
    return " ".join(words)


def _analyze_subject(subject: str) -> dict:
    """Sustantivo, marca, caracteristicas y adjetivos libres de un sujeto como
    "Piezas Lego de Pines" o "piezas con Biseles"."""
    info = {"noun": None, "gender": "f", "brand": None, "characteristics": [], "extras": []}
    for word in _WORD_RE.findall(subject or ""):
        key = fold(word)
        single = _word_key(word)
        if info["noun"] is None and single in _NOUNS:
            info["noun"], info["gender"] = _NOUNS[single]
        elif key in _BRANDS:
            info["brand"] = info["brand"] or _BRANDS[key]
        elif single in _CHARACTERISTICS:
            if _CHARACTERISTICS[single] not in info["characteristics"]:
                info["characteristics"].append(_CHARACTERISTICS[single])
        elif key in _STOPWORDS:
            continue
        else:
            info["extras"].append(word if _keep_as_written(word) else singular(word))
    return info


def _piece_name(info: dict, measure: str, suffix_words: list, default_brand: str):
    noun = info["noun"] or "Pieza"
    gender_index = 1 if info["gender"] == "f" else 2
    characteristics = [entry[gender_index] for entry in info["characteristics"]]
    brand = info["brand"] or default_brand
    words = [noun] + ([brand] if brand else []) + characteristics + info["extras"]
    words += [measure] if measure else []
    words += suffix_words
    feature = " ".join(characteristics + info["extras"]) or (noun if info["noun"] else "")
    return _collapse(" ".join(words)), (feature[:1].upper() + feature[1:]) if feature else ""


def _vague_words(text: str) -> list:
    found = []
    for word in _WORD_RE.findall(text or ""):
        if _word_key(word) in _VAGUE and word.lower() not in found:
            found.append(word.lower())
    return found


def _context_brand(*texts) -> str:
    for text in texts:
        for word in _WORD_RE.findall(fold(text)):
            if word in _CONTEXT_BRANDS:
                return _BRANDS[word]
    return ""


def _entry(kind, name, feature, source, quantity=None, notes=None, selected=True):
    return {
        "kind": kind, "name": name, "feature": feature, "source": source,
        "quantity": quantity, "notes": list(notes or []), "selected": selected,
    }


def parse_description(text, context: str = "") -> dict:
    """Interpreta la descripcion de un contenedor.

    Devuelve {"entries": [...], "warnings": [...], "unparsed": [...]} donde cada
    entrada es {"kind", "name", "feature", "source", "quantity", "notes",
    "selected"}. `quantity` es None salvo que el texto la diga ("20 piezas ...").
    `context` (categoria y nombre del contenedor) solo aporta la marca."""
    default_brand = _context_brand(text, context)
    entries, warnings, unparsed = [], [], []
    current = None          # sujeto vigente para medidas sueltas ("2x2")
    last_free = None        # producto sin medida de la parte anterior (posible encabezado)

    for part in _split_parts(text or ""):
        clean = part["clean"]
        if not _LETTER_RE.search(clean) and not _find_measures(clean):
            if not clean.isdigit():  # "1." / "2)" de una lista numerada no es un producto
                unparsed.append(clean)
            last_free = None
            continue

        quantity = None
        qty_match = _QTY_RE.match(clean)
        if qty_match:
            quantity = int(qty_match.group(1))
            clean = clean[qty_match.end():]
        notes = []
        if quantity is not None:
            notes.append(f"Cantidad tomada de la descripción ({quantity}).")
        if part["after_box_and"]:
            box_name = _sentence_case(_clean(part["prev_clean"])[0])
            notes.append(
                f"Venía unido con «y» a «{box_name}»: confirma si está dentro de esa caja "
                "o suelto en el contenedor."
            )

        if _is_box(clean):
            name = _sentence_case(clean)
            vague = _vague_words(clean)
            if vague:
                notes.append(
                    f"«{', '.join(vague)}» es un término genérico: confirma qué contiene la caja "
                    "(p. ej. qué tipo de piezas) y ajusta el nombre."
                )
            notes.append("Caja física: en Cantidad registra las piezas que contiene (o 1 si cuentas la caja).")
            entries.append(_entry(KIND_BOX, name, "Caja", clean, quantity, notes))
            current, last_free = None, None
            continue

        measures = _find_measures(clean)
        if not measures:
            name = _sentence_case(clean)
            if re.search(r"\s[ye]\s|,", clean, re.IGNORECASE):
                notes.append("Parece nombrar varios productos: si es así, divídelo o ajusta el nombre.")
            vague = _vague_words(clean)
            if vague:
                notes.append(f"«{', '.join(vague)}» es un término genérico: precisa qué producto es.")
            entry = _entry(KIND_PRODUCT, name, "", clean, quantity, notes)
            entries.append(entry)
            last_free = entry
            continue

        subject = _strip_stopword_edges(clean[:measures[0]["start"]])
        suffix = _strip_stopword_edges(clean[measures[-1]["end"]:])
        inherited = False
        suffix_info = _analyze_subject(suffix)
        if subject:
            current = subject
        elif suffix_info["noun"] or suffix_info["characteristics"]:
            # "2x2 con pines": el sujeto viene despues de la medida.
            current, suffix = suffix, ""
        elif last_free is not None and entries and entries[-1] is last_free:
            # "Piezas Lego de pines: 4x2, 2x2": la parte anterior era el encabezado.
            entries.pop()
            current = last_free["source"]
            inherited = True
        elif suffix and current is None:
            current, suffix = suffix, ""
        else:
            inherited = current is not None
        last_free = None

        info = _analyze_subject(current or "")
        suffix_words = [w if _keep_as_written(w) else singular(w) for w in suffix.split()]
        for measure in measures:
            name, feature = _piece_name(info, measure["text"], suffix_words, default_brand)
            entry_notes = list(notes)
            selected = True
            if current is None:
                entry_notes.append(
                    f"La descripción no dice qué tipo de pieza es «{measure['raw']}»: revisa el nombre."
                )
            if measure["zero"]:
                selected = False
                entry_notes.append(
                    f"La medida «{measure['raw']}» tiene una dimensión 0, que no existe; probablemente "
                    f"es «{measure['guess']}». Corrige el nombre y marca «{COL_CREATE}» para incluirla."
                )
            vague = _vague_words(current or "")
            if vague:
                entry_notes.append(f"«{', '.join(vague)}» es un término genérico: precisa qué pieza es.")
            if inherited:
                source = f"{current} … {measure['raw']}"
            elif len(measures) > 1:
                source = f"{subject} … {measure['raw']}" if subject else measure["raw"]
            else:
                source = clean
            entries.append(_entry(KIND_PIECE, name, feature, source, quantity, entry_notes, selected))

    # Un mismo producto descrito dos veces se propone una sola vez.
    unique, seen = [], {}
    for entry in entries:
        key = product_key(entry["name"])
        if key and key in seen:
            first = seen[key]["name"]
            same = "aparece más de una vez" if first == entry["name"] else f"es la misma pieza que «{first}»"
            warnings.append(f"«{entry['name']}» {same} en la descripción: se propone una sola vez.")
            continue
        seen[key] = entry
        unique.append(entry)
    for fragment in unparsed:
        warnings.append(f"No se pudo interpretar «{fragment}»: revisa la descripción.")
    return {"entries": unique, "warnings": warnings, "unparsed": unparsed}


# ---------------------------------------------------------------------------
# Codigos y ubicacion
# ---------------------------------------------------------------------------

def _safe_parse(code):
    try:
        return barcode.parse_code(str(code or "").strip())
    except ValueError:
        return None


def _standard(code):
    parsed = _safe_parse(code)
    return parsed if parsed and parsed["format"] == barcode.FORMAT_STANDARD else None


def next_child_codes(parent_code: str, used_ids, count: int):
    """`count` codigos libres y consecutivos para productos dentro del contenedor
    `parent_code`. Devuelve (codigos, nota); un codigo vacio significa que no se
    puede proponer uno y la nota explica por que.

    - Contenedor GLIOPS (2-1-01-00-000): nivel caja 2-1-01-NN-000, con NN
      despues de la caja mas alta ya usada en ese contenedor.
    - Caja GLIOPS (2-1-01-03-000): nivel item 2-1-01-03-NNN.
    - Codigo alfanumerico libre (CONT-01): CONT-01-NN si cabe en la etiqueta.
    """
    parent_code = str(parent_code or "").strip()
    used = {str(code or "").strip() for code in used_ids or ()}
    used.add(parent_code)
    if count <= 0:
        return [], ""
    parsed = _safe_parse(parent_code)
    manual = "Escribe el código de este producto: el contenedor no tiene un código del que derivarlo."

    if parsed and parsed["format"] == barcode.FORMAT_STANDARD:
        level = barcode.standard_code_level(parsed)
        if level == barcode.LEVEL_ITEM:
            return [""] * count, (
                "Escribe el código de este producto: el contenedor tiene un código de nivel ítem."
            )
        same_place = []
        for code in used:
            other = _standard(code)
            if other and all(other[k] == parsed[k] for k in ("estanteria", "piso", "contenedor")):
                same_place.append(other)
        place = (parsed["estanteria"], parsed["piso"], parsed["contenedor"])
        if level == barcode.LEVEL_CONTAINER:
            number = max((o["caja"] for o in same_place), default=0) + 1
        else:
            number = max((o["item"] for o in same_place if o["caja"] == parsed["caja"]), default=0) + 1
        codes = []
        while len(codes) < count:
            if level == barcode.LEVEL_CONTAINER:
                code = barcode.build_standard_code(*place, number, 0)
            else:
                code = barcode.build_standard_code(*place, parsed["caja"], number)
            if code not in used:
                codes.append(code)
                used.add(code)
            number += 1
        return codes, ""

    if parsed and parsed["format"] == barcode.FORMAT_ALNUM:
        codes, number = [], 1
        while len(codes) < count:
            code = f"{parent_code}-{number:02d}"
            if barcode.detect_format(code) != barcode.FORMAT_ALNUM:
                break
            if code not in used:
                codes.append(code)
                used.add(code)
            number += 1
        if len(codes) == count:
            return codes, ""
        return codes + [""] * (count - len(codes)), (
            "Escribe el código de este producto: el del contenedor es demasiado largo para derivarlo."
        )
    return [""] * count, manual


def child_location(container: dict, code: str) -> str:
    """Ubicacion de un producto dentro del contenedor: la del contenedor mas la
    caja (o item) que indica su codigo, con el mismo estilo de separadores.
    "Estantería- 2; Piso-1; Contenedor-1." + 2-1-01-04-000 ->
    "Estantería- 2; Piso-1; Contenedor-1; Caja 04."."""
    container = container or {}
    base = _collapse(container.get("location"))
    parent = _standard(container.get("id"))
    child = _standard(code)
    if not base and parent:
        base = f"Estantería {parent['estanteria']}; Piso {parent['piso']}; Contenedor {parent['contenedor']:02d}"
        if parent["caja"]:
            base += f"; Caja {parent['caja']:02d}"
    if not child:
        return base
    extra = []
    if child["caja"] and (not parent or parent["caja"] != child["caja"]):
        extra.append(f"Caja {child['caja']:02d}")
    if child["item"]:
        extra.append(f"Ítem {child['item']:03d}")
    if not extra:
        return base
    if not base:
        return "; ".join(extra)
    trailing_dot = base.endswith(".")
    core = base.rstrip(" .;,")
    separator = "; " if ";" in core else (", " if "," in core else " · ")
    return core + separator + separator.join(extra) + ("." if trailing_dot else "")


# ---------------------------------------------------------------------------
# Propuestas para un contenedor
# ---------------------------------------------------------------------------

def _active(item: dict) -> bool:
    return (item or {}).get("status") != "retired"


def container_children(container_id: str, items) -> list:
    container_id = str(container_id or "").strip()
    return [i for i in items or () if _active(i) and str(i.get("parent_id") or "").strip() == container_id]


def suggest_products(container: dict, items=()) -> dict:
    """Propuestas de Contenedores de Caracteristica para `container` a partir de
    su descripcion, sin repetir los productos que ya tiene.

    `items` debe incluir TODOS los items (tambien los dados de baja) para no
    proponer un codigo ya registrado. Devuelve:
    {"container_id", "container_name", "proposals": [...], "existing": [...],
     "warnings": [...], "unparsed": [...]}. Cada propuesta trae: kind, name,
    feature, source, notes, selected, code, parent_id, item_type ("child"),
    category, location, unit, quantity (0 si el texto no la dice),
    min_stock_alert y pending_count."""
    container = container or {}
    items = list(items or ())
    container_id = str(container.get("id") or "").strip()
    parsed = parse_description(
        container.get("description"), context=f"{container.get('category') or ''} {container.get('name') or ''}"
    )
    existing_by_key = {}
    for child in container_children(container_id, items):
        existing_by_key.setdefault(product_key(child.get("name")), child)

    proposals, existing = [], []
    for entry in parsed["entries"]:
        match = existing_by_key.get(product_key(entry["name"]))
        if match is not None:
            existing.append({"name": entry["name"], "id": match.get("id"),
                             "existing_name": match.get("name"), "source": entry["source"]})
            continue
        proposals.append(entry)

    used_ids = {str(i.get("id") or "").strip() for i in items}
    codes, code_note = next_child_codes(container_id, used_ids, len(proposals))
    for proposal, code in zip(proposals, codes):
        quantity = proposal["quantity"] if proposal["quantity"] is not None else 0
        proposal.update(
            code=code, parent_id=container_id, item_type="child",
            category=container.get("category") or "", location=child_location(container, code),
            unit=DEFAULT_UNIT, quantity=quantity, min_stock_alert=0, pending_count=quantity == 0,
        )
        if not code and code_note:
            proposal["notes"].append(code_note)
    return {
        "container_id": container_id,
        "container_name": container.get("name") or "",
        "proposals": proposals,
        "existing": existing,
        "warnings": parsed["warnings"],
        "unparsed": parsed["unparsed"],
    }


def editor_records(proposals) -> list:
    """Filas de la vista previa editable (st.data_editor), en el orden de las
    propuestas."""
    return [
        {
            COL_CREATE: bool(p.get("selected", True)),
            COL_CODE: p.get("code") or "",
            COL_NAME: p.get("name") or "",
            COL_FEATURE: p.get("feature") or "",
            COL_QUANTITY: int(p.get("quantity") or 0),
            COL_UNIT: p.get("unit") or DEFAULT_UNIT,
            COL_NOTES: " ".join(p.get("notes") or []),
        }
        for p in proposals or ()
    ]


def rows_from_records(records, proposals) -> list:
    """Une lo editado por el usuario con los datos de origen de cada propuesta
    (fragmento de la descripcion, notas). Las filas se emparejan por posicion:
    la vista previa no permite agregar ni borrar filas."""
    rows = []
    proposals = list(proposals or ())
    for index, record in enumerate(records or ()):
        proposal = proposals[index] if index < len(proposals) else {}
        rows.append({
            "selected": record.get(COL_CREATE, False),
            "code": record.get(COL_CODE, ""),
            "name": record.get(COL_NAME, ""),
            "quantity": record.get(COL_QUANTITY, 0),
            "unit": record.get(COL_UNIT, DEFAULT_UNIT),
            "source": proposal.get("source", ""),
            "feature": proposal.get("feature", ""),
        })
    return rows


def _is_missing(value) -> bool:
    """None, NaN, pd.NA/NaT o texto vacio (celda borrada en la vista previa)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return str(value) in ("<NA>", "NaT", "nan")


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "si", "sí", "x")
    if _is_missing(value):
        return False
    try:
        return bool(value)
    except (TypeError, ValueError):
        return False


def _as_quantity(value):
    """(cantidad, error). Vacio cuenta como 0 (pendiente de conteo)."""
    if _is_missing(value):
        return 0, None
    if isinstance(value, bool):
        return None, "la cantidad debe ser un número entero"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, "la cantidad debe ser un número entero"
    if math.isnan(number):
        return 0, None
    if not number.is_integer():
        return None, "la cantidad debe ser un número entero"
    if number < 0:
        return None, "la cantidad no puede ser negativa"
    return int(number), None


def _hierarchy_error(code: str, container_id: str):
    """El codigo GLIOPS de un producto debe quedar DENTRO del contenedor."""
    parent = _standard(container_id)
    child = _standard(code)
    if not parent:
        return None
    place = f"{parent['estanteria']}-{parent['piso']}-{parent['contenedor']:02d}"
    if parent["caja"] == 0:
        expected = f"{place}-NN-000"
        ok = child and child["caja"] > 0 and child["item"] == 0
    elif parent["item"] == 0:
        expected = f"{place}-{parent['caja']:02d}-NNN"
        ok = child and child["caja"] == parent["caja"] and child["item"] > 0
    else:
        return None
    same_place = child and all(child[k] == parent[k] for k in ("estanteria", "piso", "contenedor"))
    if ok and same_place:
        return None
    return f"el código {code} no queda dentro del contenedor {container_id} (usa {expected})"


def build_payload(row: dict, container: dict) -> dict:
    """Datos para LabStorage.save_items_bulk de una fila ya validada."""
    container = container or {}
    container_id = str(container.get("id") or "").strip()
    code = str(row.get("code") or "").strip()
    quantity = int(row.get("quantity") or 0)
    source = _collapse(row.get("source"))
    description = f"Creado desde la descripción de «{container.get('name') or container_id}» ({container_id})"
    description += f": «{source}»." if source else "."
    if quantity == 0:
        description += f" {PENDING_COUNT_NOTE}"
    return {
        "id": code,
        "name": _collapse(row.get("name")),
        "category": container.get("category") or "",
        "description": description,
        "item_type": "child",
        "parent_id": container_id,
        "unit": _collapse(row.get("unit")) or DEFAULT_UNIT,
        "quantity": quantity,
        "location": child_location(container, code),
        "min_stock_alert": 0,
        "status": "active",
    }


def validate_rows(rows, container: dict, items=()) -> dict:
    """Valida las filas marcadas para crear con las reglas del inventario
    (formato GLIOPS, codigo libre, jerarquia dentro del contenedor, nombre y
    cantidad) y sin repetir productos del contenedor ni de la propia tanda.

    Devuelve {"payloads": [...], "errors": [...], "warnings": [...],
    "selected": n}. Solo hay payloads utiles si `errors` esta vacio."""
    container = container or {}
    container_id = str(container.get("id") or "").strip()
    items = list(items or ())
    active_by_id = {str(i.get("id") or "").strip(): i for i in items if _active(i)}
    child_by_key = {}
    for child in container_children(container_id, items):
        child_by_key.setdefault(product_key(child.get("name")), child)

    payloads, errors, warnings = [], [], []
    seen_codes, seen_keys = {}, {}
    selected = 0
    for position, row in enumerate(rows or (), start=1):
        if not _truthy(row.get("selected")):
            continue
        selected += 1
        name = _collapse(row.get("name"))
        code = str(row.get("code") or "").strip()
        label = f"Fila {position}" + (f" ({name})" if name else "")
        problems = []

        if not name:
            problems.append("el nombre es obligatorio")
        if not code:
            problems.append("el código es obligatorio")
        else:
            try:
                barcode.validate_code_format(code)
            except ValueError as exc:
                problems.append(str(exc))
            else:
                hierarchy = _hierarchy_error(code, container_id)
                if hierarchy:
                    problems.append(hierarchy)
            if code == container_id:
                problems.append("no puede usar el mismo código del contenedor")
            elif code in seen_codes:
                problems.append(f"el código {code} está repetido (fila {seen_codes[code]})")
            elif code in active_by_id:
                problems.append(f"el código {code} ya pertenece a «{active_by_id[code].get('name')}»")
            seen_codes.setdefault(code, position)

        quantity, quantity_error = _as_quantity(row.get("quantity"))
        if quantity_error:
            problems.append(quantity_error)

        if name:
            key = product_key(name)
            if key and key in child_by_key:
                existing = child_by_key[key]
                problems.append(f"ya existe «{existing.get('name')}» en este contenedor ({existing.get('id')})")
            elif key and key in seen_keys:
                problems.append(f"repite el producto de la fila {seen_keys[key]}")
            seen_keys.setdefault(key, position)
            for measure in zero_measures(name):
                warnings.append(
                    f"{label}: la medida «{measure['raw']}» tiene una dimensión 0; "
                    f"¿quisiste decir «{measure['guess']}»?"
                )

        if problems:
            errors.append(f"{label}: " + "; ".join(problem.rstrip(".") for problem in problems) + ".")
            continue
        payloads.append(build_payload({**row, "name": name, "code": code, "quantity": quantity}, container))
    return {"payloads": payloads, "errors": errors, "warnings": warnings, "selected": selected}


def is_pending_count(item: dict) -> bool:
    """Producto creado sin cantidad (pendiente de conteo) que aun no se ha contado."""
    item = item or {}
    try:
        quantity = int(float(item.get("quantity") or 0))
    except (TypeError, ValueError):
        quantity = 0
    return (item.get("item_type") != "master" and quantity == 0
            and PENDING_COUNT_NOTE in str(item.get("description") or ""))
