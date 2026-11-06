# -*- coding: utf-8 -*-
"""core/nfc.py - Chips NFC para la trazabilidad y el conteo de inventario.

Cada chip (NTAG213/215/216) guarda UN registro NDEF de tipo URI que apunta a la
app con el codigo del producto o de la ubicacion:

    https://<app>/escanear?nfc=2-1-01-01-001&s=K7Q2MX9AB0

Al acercar el telefono (Android o iPhone, sin instalar nada) el sistema abre esa
URL en el navegador. La app guarda el toque (prueba de que la persona estuvo
frente al producto), muestra el producto y, si quien toca tiene una ruta
verificable o hay un conteo de inventario abierto, lo aplica alli (ver
`handle_tap` y views/nfc_tap.py).

- `s` es una firma corta (HMAC-SHA256 con el secreto NFC_SECRET) para que una
  URL no se pueda inventar escribiendo un codigo a mano. Sin NFC_SECRET las URL
  van sin firma y se aceptan igual (el codigo de barras impreso ya es publico).
- El registro de chips (cuales se grabaron y cuales ya se probaron) se deriva de
  eventos de la hoja `trace_events`: grabado, retirado y cada toque. No hay
  hoja nueva en la base.

Logica pura (sin Streamlit): se prueba con FakeStorage.
"""

import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

from core import barcode, inventory_count, location, traceability
from core.config import safe_secret
from core.service_requests import STATUS_APPROVED, TYPE_PRODUCT

QUERY_CODE = "nfc"          # parametro de la URL con el codigo
QUERY_SIGNATURE = "s"       # parametro de la URL con la firma
QUERY_KID = "k"             # M4: version de la clave con que se firmo (rotacion); sin k = NFC_SECRET de siempre
QUERY_MIRROR = "m"          # M4: espejo ASCII de UID y contador que el NTAG21x escribe en cada lectura
MIRROR_PLACEHOLDER = "0" * 14 + "x" + "0" * 6   # 21 caracteres que el chip reemplaza por UID 'x' contador
MAX_KID_LENGTH = 16
TAG_PAGE = "escanear"       # pagina a la que apunta la URL (si algo falla, se cae en Escanear)
MAX_CODE_LENGTH = 64
SIGNATURE_LENGTH = 10       # 10 caracteres Crockford = 50 bits
DEDUP_SECONDS = 60          # el mismo chip tocado dos veces seguidas cuenta una sola vez
_SIGNATURE_VERSION = "lab-nfc-v1"
_ALPHABET = traceability.RECEIPT_ALPHABET  # sin I, L, O ni U

# Resultado de comprobar la firma de un toque.
SIG_UNSIGNED = "unsigned"   # no hay NFC_SECRET: se acepta sin firma
SIG_VALID = "valid"
SIG_MISSING = "missing"     # hay NFC_SECRET y la URL no trae firma
SIG_INVALID = "invalid"     # la firma no corresponde al codigo
SIGNATURE_MESSAGES = {
    SIG_MISSING: "Este chip se grabó sin firma y la app exige firma (NFC_SECRET): el toque no se registró. "
                 "Vuelve a grabarlo con la URL de Chips NFC → Grabar etiquetas.",
    SIG_INVALID: "La firma de este chip no es válida (la URL se alteró o se grabó con otro NFC_SECRET): el "
                 "toque no se registró. Vuelve a grabarlo con la URL de Chips NFC → Grabar etiquetas.",
}
SIG_UNKNOWN_KEY = "unknown_key"     # M4: la URL trae una version de clave (k) que no esta en NFC_KEYS
SIGNATURE_MESSAGES[SIG_UNKNOWN_KEY] = (
    "Este chip se firmó con una versión de clave (k) que la app no tiene configurada en NFC_KEYS: el toque no "
    "se registró. Revisa los Secrets o vuelve a grabar el chip.")

# M4: anomalias del espejo NTAG21x (UID y contador de lecturas).
ANOMALY_COUNTER = "counter_not_increasing"   # el contador no crecio: URL copiada o repetida
ANOMALY_UID = "uid_changed"                  # otro UID para el mismo codigo: chip duplicado
ANOMALY_MISSING = "mirror_missing"           # un chip que ya enviaba espejo llego sin el
ANOMALY_LABELS = {
    ANOMALY_COUNTER: "el contador de lecturas del chip no aumentó (posible URL copiada o repetida)",
    ANOMALY_UID: "el UID no es el del chip registrado (posible chip duplicado)",
    ANOMALY_MISSING: "la URL llegó sin el contador que este chip siempre envía (posible URL editada)",
}
REJECT_ANOMALY = "anomaly"                   # motivo de un toque bloqueado por el modo estricto
STALE_DAYS = 30                              # «sin lectura reciente» en la salud de chips

# Capacidad NDEF util (bytes) de los chips NTAG mas comunes.
CHIP_CAPACITY = {"NTAG213": 137, "NTAG215": 496, "NTAG216": 868}
RECOMMENDED_CHIP = "NTAG215"
# Abreviaturas de prefijo del registro URI (NFC Forum): ocupan 1 byte.
_URI_PREFIXES = ("https://www.", "http://www.", "https://", "http://")

# A donde lleva un toque despues de procesarlo (claves de las paginas en app.py).
TARGET_SCAN = "escanear"
TARGET_ROUTE = "trazabilidad"
TARGET_COUNT = "nfc"

TAG_WRITTEN = "written"     # grabado (todavia nadie lo ha tocado)
TAG_TESTED = "tested"       # ya se toco al menos una vez: funciona
TAG_STATUS_LABELS = {TAG_WRITTEN: "Grabado (sin probar)", TAG_TESTED: "Probado"}
NO_TAG_LABEL = "Sin chip"


# ---------------------------------------------------------------------------
# Configuracion (Secrets)
# ---------------------------------------------------------------------------

def nfc_secret() -> str:
    """Secreto para firmar las URL de los chips (vacio = sin firma)."""
    return str(safe_secret("NFC_SECRET", "") or "").strip()


def nfc_keys() -> dict:
    """M4: claves versionadas de los Secrets (tabla ``[NFC_KEYS]``, version -> secreto) para rotar la clave sin
    invalidar los chips ya grabados. Vacio = sin rotacion. Las claves nunca estan en el codigo."""
    raw = safe_secret("NFC_KEYS", None)
    try:
        pairs = dict(raw or {}).items()
    except (TypeError, ValueError):
        return {}
    keys = {}
    for kid, secret in pairs:
        kid, secret = str(kid or "").strip(), str(secret or "").strip()
        if kid and secret and len(kid) <= MAX_KID_LENGTH:
            keys[kid] = secret
    return keys


def active_kid() -> str:
    """M4: version de clave para grabar chips NUEVOS (Secret ``NFC_KID``). '' = NFC_SECRET sin ``k``, como antes."""
    kid = str(safe_secret("NFC_KID", "") or "").strip()
    return kid if kid in nfc_keys() else ""


def signing_key() -> tuple:
    """(kid, secreto) con que se firman los chips nuevos: la clave activa o, sin rotacion, NFC_SECRET."""
    kid = active_kid()
    return (kid, nfc_keys()[kid]) if kid else ("", nfc_secret())


def audit_enabled(storage) -> bool:
    from core import options

    return options.enabled(storage, options.NFC_AUDIT)


def strict_enabled(storage) -> bool:
    """El modo estricto solo actua con la auditoria activa."""
    from core import options

    return audit_enabled(storage) and options.enabled(storage, options.NFC_STRICT)


def _clean_base(value) -> str:
    value = str(value or "").strip().rstrip("/")
    parts = urlsplit(value)
    return value if parts.scheme in ("http", "https") and parts.netloc else ""


def configured_app_url() -> str:
    """APP_URL de los Secrets (direccion publica de la app) o "" si no es valida."""
    return _clean_base(safe_secret("APP_URL", ""))


def origin_of(url) -> str:
    """scheme://host de una URL (la que ve el navegador, st.context.url)."""
    parts = urlsplit(str(url or "").strip())
    if parts.scheme in ("http", "https") and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return ""


def app_base_url(context_url: str = None) -> str:
    """Direccion base de las URL de los chips: APP_URL si esta configurada; si no,
    el origen de la direccion con la que se abrio la app."""
    return configured_app_url() or origin_of(context_url)


# ---------------------------------------------------------------------------
# Firma, URL y lectura del toque
# ---------------------------------------------------------------------------

def sign_code(code: str, secret: str) -> str:
    """Firma corta y determinista de `code` ("" sin secreto)."""
    if not secret:
        return ""
    payload = f"{_SIGNATURE_VERSION}|{code}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    number = int.from_bytes(digest[:8], "big")
    chars = []
    for _ in range(SIGNATURE_LENGTH):
        number, remainder = divmod(number, len(_ALPHABET))
        chars.append(_ALPHABET[remainder])
    return "".join(chars)


def check_signature(code: str, signature: str, secret: str) -> str:
    """SIG_UNSIGNED, SIG_VALID, SIG_MISSING o SIG_INVALID."""
    if not secret:
        return SIG_UNSIGNED
    signature = str(signature or "").strip().upper()
    if not signature:
        return SIG_MISSING
    # En bytes: compare_digest con texto no ASCII lanza TypeError (una firma escrita a mano no debe tumbar el toque).
    expected = sign_code(code, secret)
    return SIG_VALID if hmac.compare_digest(signature.encode("utf-8"), expected.encode("utf-8")) else SIG_INVALID


def signature_accepted(status: str) -> bool:
    return status in (SIG_UNSIGNED, SIG_VALID)


def validate_tag_code(code) -> str:
    """Codigo listo para grabar: sin espacios alrededor y con un formato valido
    de core/barcode.py (lanza ValueError con el motivo)."""
    code = str(code or "").strip()
    if not code:
        raise ValueError("El código está vacío.")
    if barcode.location_code(code):
        return code        # estantería, piso, mesa o zona: sus códigos (2-1-00-00-000…) no son de producto, pero llevan chip
    barcode.validate_code_format(code)
    return code


def build_tag_url(code, base_url: str, secret: str = "", kid: str = "", mirror: bool = False) -> str:
    """URL que se graba en el chip: <base>/escanear?nfc=<codigo>[&s=<firma>].

    M4 (opcional, por omision no cambia nada): ``kid`` agrega ``&k=<version>`` de la clave con que se firmo, y
    ``mirror`` agrega ``&m=`` con 21 caracteres que el NTAG21x reemplaza por su UID y su contador de lecturas."""
    code = validate_tag_code(code)
    base = _clean_base(base_url)
    if not base:
        raise ValueError("Falta la dirección pública de la app: configura APP_URL en los Secrets.")
    url = f"{base}/{TAG_PAGE}?{QUERY_CODE}={quote(code, safe='')}"
    signature = sign_code(code, secret)
    if signature:
        url = f"{url}&{QUERY_SIGNATURE}={signature}"
        if kid:
            url = f"{url}&{QUERY_KID}={quote(str(kid), safe='')}"
    if mirror:
        # POR PROBAR: con chips NTAG213/215/216 reales. Al grabar (p. ej. con NXP TagWriter) hay que activar el
        # espejo de UID y de contador (MIRROR_CONF = UID + contador, NFC_CNT_EN = 1) apuntando a estos 21
        # caracteres; si no se activa, llegan los ceros y la app lo trata como «espejo sin configurar».
        url = f"{url}&{QUERY_MIRROR}={MIRROR_PLACEHOLDER}"
    return url


def ndef_size(url: str) -> int:
    """Bytes que ocupa la URL como mensaje NDEF (un registro URI corto)."""
    raw = str(url or "")
    rest = len(raw.encode("utf-8"))
    for prefix in _URI_PREFIXES:
        if raw.startswith(prefix):
            rest -= len(prefix)
            break
    payload = 1 + rest  # 1 byte de prefijo abreviado
    header = 4 if payload < 256 else 7  # banderas + largo del tipo + largo del contenido + tipo "U"
    return header + payload


def chips_that_fit(url: str) -> list:
    size = ndef_size(url)
    return [chip for chip, capacity in CHIP_CAPACITY.items() if size <= capacity]


def _first(value) -> str:
    if isinstance(value, (list, tuple)):
        value = value[-1] if value else ""
    return "" if value is None else str(value)


def parse_tap(params):
    """Toque NFC desde los parametros de la URL: {"code", "signature", "error"};
    None si la URL no trae ?nfc=."""
    if not params or QUERY_CODE not in params:
        return None
    code = _first(params.get(QUERY_CODE)).strip()
    signature = _first(params.get(QUERY_SIGNATURE)).strip()[:32]
    error = ""
    if not code:
        error = "El chip NFC no trae ningún código."
    elif len(code) > MAX_CODE_LENGTH or not code.isprintable() or re.search(r"\s", code):
        error = "El chip NFC trae un código no válido."
        code = ""
    tap = {"code": code, "signature": signature, "error": error}
    # M4: solo si la URL los trae (las URL de siempre devuelven exactamente las tres claves de arriba).
    if QUERY_KID in params:
        tap["kid"] = _first(params.get(QUERY_KID)).strip()[:MAX_KID_LENGTH]
    if QUERY_MIRROR in params:
        tap["mirror"] = _first(params.get(QUERY_MIRROR)).strip()[:32]
    return tap


# --- M4: espejo ASCII del NTAG21x (UID y contador de lecturas) -------------------------------
#
# Con el espejo activo, el chip escribe en la URL, en cada lectura, su UID (7 bytes = 14 hex) y su
# contador de lecturas NFC (3 bytes = 6 hex) separados por «x». El espejo NO va firmado: sirve para
# detectar URLs copiadas o repetidas y chips duplicados, no para probar la originalidad del chip.
# POR PROBAR: la firma de originalidad de NXP (READ_SIG, ECC) no se puede leer desde el navegador del
# celular (no hay acceso a comandos NFC de bajo nivel); no se simula aqui.

_MIRROR_RE = re.compile(r"^(?:([0-9A-Fa-f]{14})x([0-9A-Fa-f]{6})|([0-9A-Fa-f]{6})|([0-9A-Fa-f]{14}))$")


def parse_mirror(value) -> dict:
    """``{"uid", "counter", "configured"}`` desde el espejo (UID 'x' contador, o solo uno de los dos); ``{}`` si
    no hay espejo o no tiene ese formato. Los ceros del molde = el chip no tiene el espejo activado."""
    match = _MIRROR_RE.match(str(value or "").strip())
    if not match:
        return {}
    uid = (match.group(1) or match.group(4) or "").upper()
    counter_hex = match.group(2) or match.group(3) or ""
    counter = int(counter_hex, 16) if counter_hex else None
    return {"uid": uid, "counter": counter, "configured": bool(uid.strip("0")) or bool(counter)}


def mirror_anomalies(previous_taps: list, mirror: dict) -> list:
    """Anomalias de un toque frente a los toques ACEPTADOS anteriores del mismo chip (en orden de hora).

    Un chip sin historial de espejo no tiene contra que compararse. Uno que ya envio espejo debe llegar con el
    mismo UID y con un contador mayor que el ultimo visto."""
    seen = []
    for tap in previous_taps or []:
        item = traceability.event_details(tap).get("mirror")
        if isinstance(item, dict) and item.get("configured"):
            seen.append(item)
    if not seen:
        return []
    if not mirror or not mirror.get("configured"):
        return [ANOMALY_MISSING]
    anomalies = []
    uids = {item.get("uid") for item in seen if item.get("uid")}
    if mirror.get("uid") and uids and mirror["uid"] not in uids:
        anomalies.append(ANOMALY_UID)
    same_chip = [item for item in seen if not mirror.get("uid") or item.get("uid") == mirror.get("uid")]
    counters = [item["counter"] for item in same_chip if isinstance(item.get("counter"), int)]
    if isinstance(mirror.get("counter"), int) and counters and mirror["counter"] <= max(counters):
        anomalies.append(ANOMALY_COUNTER)
    return anomalies


def anomaly_text(anomalies: list, blocked: bool = False) -> str:
    reasons = "; ".join(ANOMALY_LABELS.get(kind, kind) for kind in anomalies or [])
    if blocked:
        return f"Toque bloqueado (modo estricto): {reasons}. Quedó registrado para revisión."
    return f"Alerta registrada: {reasons}."


# ---------------------------------------------------------------------------
# Toques (eventos nfc_tap)
# ---------------------------------------------------------------------------

def _now(now=None) -> datetime:
    return traceability.to_datetime(now) or datetime.now(timezone.utc)


def _actor_fields(actor: dict) -> dict:
    actor = actor or {}
    return {
        "actor_id": actor.get("id", ""), "actor_name": actor.get("full_name", ""),
        "actor_email": actor.get("institutional_email", ""),
        "actor_role": actor.get("role", ""),
    }


def tap_code(event: dict) -> str:
    return str((event or {}).get("item_id") or traceability.event_details(event).get("code") or "")


# Dónde van los chips: solo estanterías, pisos y contenedores. Al tocarlos la app reconoce que la persona llegó.
ARRIVAL_SHELF, ARRIVAL_FLOOR, ARRIVAL_CONTAINER = "estantería", "piso", "contenedor"
_ARRIVAL_ARTICLE = {ARRIVAL_SHELF: "a la", ARRIVAL_FLOOR: "al", ARRIVAL_CONTAINER: "al"}


def arrival_kind(item: dict) -> str:
    """«estantería», «piso» o «contenedor» si ``item`` es uno de los lugares donde van chips NFC; si no, ''."""
    item = item or {}
    if item.get("item_type") == "master":
        return ARRIVAL_CONTAINER
    if item.get("item_type") == "location":
        place = barcode.location_code(str(item.get("id") or ""))
        if place and place.get("kind") == barcode.LOCATION_SHELF:
            return ARRIVAL_SHELF
        if place and place.get("kind") == barcode.LOCATION_FLOOR:
            return ARRIVAL_FLOOR
    return ""


def arrival_message(storage, item: dict) -> str:
    """«Llegaste al contenedor «X» (código) · 5 producto(s).» o '' si el chip no es de un lugar."""
    kind = arrival_kind(item)
    if not kind:
        return ""
    text = f"Llegaste {_ARRIVAL_ARTICLE[kind]} {kind} «{item.get('name') or item.get('id')}» ({item.get('id')})"
    if kind == ARRIVAL_CONTAINER:
        try:
            count = len(storage.get_children(item["id"]))
        except Exception:
            count = 0
        text += f" · {count} producto(s) dentro" if count else " · todavía sin productos"
    return text


def record_tap(storage, user: dict, code: str, item: dict = None, signed: bool = False,
               count_session: str = "", now=None, extra: dict = None):
    """Guarda UN evento `nfc_tap` y devuelve (evento, repetido).

    Si la misma persona toco el mismo chip hace menos de DEDUP_SECONDS (en el
    mismo contexto), no se escribe otra vez: un doble toque o una recarga no
    cuentan doble. ``extra`` (M4: espejo y anomalias) va en los detalles; un toque
    con anomalias se escribe siempre (la alerta no se pierde)."""
    now = _now(now)
    previous = storage.get_trace_events(user_id=(user or {}).get("id", ""), item_id=code,
                                        event_type=traceability.EVENT_NFC_TAP)
    if previous and not (extra or {}).get("anomalies"):
        last = previous[-1]
        when = traceability.to_datetime(last.get("created_at"))
        same_context = traceability.event_details(last).get("count_session", "") == (count_session or "")
        if when and same_context and 0 <= (now - when).total_seconds() < DEDUP_SECONDS:
            return last, True
    details = {"code": code, "item_name": (item or {}).get("name", ""), "signed": bool(signed)}
    if arrival_kind(item):
        details["arrival"] = arrival_kind(item)
    if count_session:
        details["count_session"] = count_session
    if extra:
        details.update(extra)
    event = storage.add_trace_event({
        "event_type": traceability.EVENT_NFC_TAP, "request_id": "", "item_id": code, "loan_id": "",
        "user_id": (user or {}).get("id", ""), **_actor_fields(user), "receipt": "",
        "details": json.dumps(details, ensure_ascii=False), "created_at": now.isoformat(),
    })
    return event, False


def user_taps(storage, user_id: str, since=None) -> list:
    """Toques de una persona (del mas antiguo al mas reciente), opcionalmente desde `since`."""
    if not user_id:
        return []
    rows = storage.get_trace_events(user_id=user_id, event_type=traceability.EVENT_NFC_TAP)
    since = traceability.to_datetime(since)
    if since is None:
        return rows
    return [row for row in rows if (traceability.to_datetime(row.get("created_at")) or since) >= since]


def recent_taps(storage, limit: int = 100) -> list:
    """Ultimos toques de todo el laboratorio (el mas reciente primero)."""
    rows = storage.get_trace_events(event_type=traceability.EVENT_NFC_TAP)
    return list(reversed(rows))[:limit]


# ---------------------------------------------------------------------------
# Ruta verificable: un toque confirma el punto de control de su etiqueta
# ---------------------------------------------------------------------------

def _route_parts(storage, request: dict):
    item = storage.get_item(request.get("item_id")) or {"id": request.get("item_id"), "name": request.get("item_name")}
    parent = storage.get_item(item.get("parent_id")) if item.get("parent_id") else None
    return item, parent


def open_route_requests(storage, user: dict) -> list:
    """Solicitudes de producto aprobadas de `user` sin retirar y sin ruta guardada
    (la mas antigua primero): las que todavia se pueden recorrer."""
    if not user or not user.get("id"):
        return []
    rows = [
        row for row in storage.get_service_requests(status=STATUS_APPROVED, user_id=user["id"])
        if row.get("request_type") == TYPE_PRODUCT
    ]
    if not rows:
        return []
    done = {
        event.get("request_id")
        for event in storage.get_trace_events(user_id=user["id"])
        if event.get("event_type") in (traceability.EVENT_PICKED_UP, traceability.EVENT_ROUTE_VERIFIED)
    }
    aware_min = datetime.min.replace(tzinfo=timezone.utc)
    rows = [row for row in rows if row.get("id") not in done]
    return sorted(rows, key=lambda row: traceability.to_datetime(row.get("created_at")) or aware_min)


def route_label_codes(storage, request: dict) -> list:
    item, parent = _route_parts(storage, request)
    return [point["label_code"] for point in location.build_route_checkpoints(item, parent) if point.get("label_code")]


def requests_for_code(storage, user: dict, code: str) -> list:
    """Rutas abiertas de `user` en las que `code` es una de las etiquetas del camino."""
    return [
        request for request in open_route_requests(storage, user)
        if any(location.codes_match(label, code) for label in route_label_codes(storage, request))
    ]


def route_mismatch_hint(storage, request: dict, code: str, known_item: dict = None) -> str:
    """Orientacion cuando el chip tocado no es parte de la ruta de `request`."""
    item, parent = _route_parts(storage, request)
    anchor = location.route_anchor(item, parent)
    where = str(item.get("location") or (parent or {}).get("location") or "")
    hint = location.explain_mismatch(anchor, code, known_item, where, str(item.get("id") or ""))
    title = traceability.request_title(request)
    return f"Ese chip no es parte de tu ruta hacia «{title}». {hint}"


def _iso_seconds(value) -> str:
    dt = traceability.to_datetime(value)
    return dt.isoformat(timespec="seconds") if dt else ""


def merge_taps_into_route(route: dict, taps: list):
    """Aplica a `route` (estado de la sesion) los toques NFC que todavia no se
    aplicaron y cuyo codigo es una etiqueta del camino, en orden de hora.

    Es idempotente: recuerda los ids aplicados en route["nfc_taps"], asi que se
    puede llamar en cada recarga (sirve aunque el toque se hizo en otra pestaña).
    Devuelve el resultado (ver traceability.apply_code) del ultimo toque que hizo
    avanzar la ruta, o None si no cambio nada."""
    if not route:
        return None
    seen = set(route.get("nfc_taps") or [])
    labels = [point.get("label_code") for point in route.get("checkpoints") or [] if point.get("label_code")]
    aware_min = datetime.min.replace(tzinfo=timezone.utc)
    last = None
    for tap in sorted(taps or [], key=lambda row: traceability.to_datetime(row.get("created_at")) or aware_min):
        tap_id = tap.get("id")
        if not tap_id or tap_id in seen:
            continue
        seen.add(tap_id)
        code = tap_code(tap)
        if not any(location.codes_match(label, code) for label in labels):
            continue
        before = json.dumps(route.get("checkpoints"), sort_keys=True, default=str)
        result = traceability.apply_code(route, code, now=tap.get("created_at"), method=traceability.METHOD_NFC)
        stamp = _iso_seconds(tap.get("created_at"))
        if stamp and route.get("started_at") and stamp < route["started_at"]:
            route["started_at"] = stamp  # el toque fue antes que el primer paso de esta sesion
        if result.get("ok") and json.dumps(route.get("checkpoints"), sort_keys=True, default=str) != before:
            last = result
    route["nfc_taps"] = sorted(seen)
    return last


def merge_user_taps(storage, user: dict, request: dict, route: dict):
    """`merge_taps_into_route` con los toques de `user` desde que se aprobo `request`."""
    if not user or not request or not route:
        return None
    since = request.get("reviewed_at") or request.get("created_at")
    return merge_taps_into_route(route, user_taps(storage, user.get("id"), since))


# ---------------------------------------------------------------------------
# Que hacer con un toque (lo decide la logica; la vista solo navega)
# ---------------------------------------------------------------------------

def record_rejected(storage, user: dict, code: str, reason: str, extra: dict = None, now=None):
    """M4: guarda un toque RECHAZADO (`nfc_tap_rejected`) y devuelve el evento, o ``None`` si es la misma persona
    repitiendo el mismo rechazo hace menos de DEDUP_SECONDS (una recarga no ensucia la auditoria)."""
    now = _now(now)
    code = str(code or "")[:MAX_CODE_LENGTH]
    previous = storage.get_trace_events(user_id=(user or {}).get("id", ""), item_id=code,
                                        event_type=traceability.EVENT_NFC_TAP_REJECTED)
    if previous:
        last = previous[-1]
        when = traceability.to_datetime(last.get("created_at"))
        if (when and traceability.event_details(last).get("reason") == reason
                and 0 <= (now - when).total_seconds() < DEDUP_SECONDS):
            return None
    details = {"code": code, "reason": reason, **(extra or {})}
    return storage.add_trace_event({
        "event_type": traceability.EVENT_NFC_TAP_REJECTED, "request_id": "", "item_id": code, "loan_id": "",
        "user_id": (user or {}).get("id", ""), **_actor_fields(user), "receipt": "",
        "details": json.dumps(details, ensure_ascii=False), "created_at": now.isoformat(),
    })


def _signature_status(code: str, signature: str, secret, kid: str) -> str:
    if kid:      # M4: la URL dice con que version de clave se firmo
        key = nfc_keys().get(kid)
        return SIG_UNKNOWN_KEY if key is None else check_signature(code, signature, key)
    return check_signature(code, signature, nfc_secret() if secret is None else secret)


def handle_tap(storage, user: dict, code: str, signature: str = "", secret: str = None, now=None,
               kid: str = "", mirror: str = "") -> dict:
    """Procesa el toque de `user` sobre el chip de `code`.

    Devuelve {"ok", "tone", "message", "target", "code", "item", "event",
    "duplicate", "request_id", "count_session", "signature"}. `target` es la
    pagina a mostrar: TARGET_COUNT (profesor/maestro con un conteo abierto),
    TARGET_ROUTE (estudiante con una ruta que pasa por esa etiqueta) o TARGET_SCAN.
    Solo se guarda el toque si la firma es aceptada y el codigo existe.

    M4: ``kid`` elige la clave versionada (NFC_KEYS) con que se verifica la firma. Con ``nfc_auditoria`` activo
    se registran los toques rechazados y se revisa el espejo ``mirror`` (UID y contador); con
    ``nfc_modo_estricto`` ademas se bloquea el toque anomalo. Apagado, el espejo se ignora como siempre."""
    code = str(code or "").strip()
    status = _signature_status(code, signature, secret, kid)
    outcome = {"ok": False, "tone": "warning", "message": "", "target": TARGET_SCAN, "code": code,
               "item": None, "event": None, "duplicate": False, "request_id": "", "count_session": "",
               "signature": status}
    if not code:
        return {**outcome, "message": "El chip NFC no trae ningún código."}
    audit = audit_enabled(storage)
    if not signature_accepted(status):
        if audit:
            record_rejected(storage, user, code, status, {"kid": kid} if kid else None, now)
        return {**outcome, "message": SIGNATURE_MESSAGES[status]}
    item = storage.get_item(code)
    if not item or item.get("status") == "retired":
        return {**outcome, "message": f"El chip apunta a «{code}», que no está registrado en el inventario."}
    outcome["item"] = item
    extra, anomalies = {}, []
    if audit:
        parsed = parse_mirror(mirror)
        if parsed:
            extra["mirror"] = parsed
        if kid:
            extra["kid"] = kid
        anomalies = mirror_anomalies(
            storage.get_trace_events(item_id=code, event_type=traceability.EVENT_NFC_TAP), parsed)
        if anomalies:
            extra["anomalies"] = anomalies
            if strict_enabled(storage):
                event = record_rejected(storage, user, code, REJECT_ANOMALY, extra, now)
                return {**outcome, "tone": "danger", "event": event, "anomalies": anomalies,
                        "message": anomaly_text(anomalies, blocked=True)}
    result = _apply_tap(storage, user, code, item, status == SIG_VALID, outcome, now, extra)
    if anomalies:
        result = {**result, "tone": "warning", "anomalies": anomalies,
                  "message": f"{result['message']} {anomaly_text(anomalies)}"}
    return result


def _apply_tap(storage, user: dict, code: str, item: dict, signed: bool, outcome: dict, now, extra: dict) -> dict:
    """Registra un toque aceptado y decide a donde lleva (conteo, ruta o Escanear)."""
    name = item.get("name") or code
    repeat = " (ya lo habías tocado hace un momento: cuenta una sola vez)"

    manager = traceability.is_manager(user)
    session = inventory_count.open_session(storage) if manager else None
    if session:
        blocked = inventory_count.location_blocked(storage, session, item)      # M6: '' con la opción apagada
        if blocked:
            return {**outcome, "tone": "warning", "target": TARGET_COUNT, "count_session": session["id"],
                    "message": blocked}
        event, duplicate = record_tap(storage, user, code, item, signed, count_session=session["id"], now=now,
                                      extra=extra or None)
        message = f"«{name}» quedó verificado en el conteo «{session['name']}»"
        return {**outcome, "ok": True, "tone": "success", "target": TARGET_COUNT, "event": event,
                "duplicate": duplicate, "count_session": session["id"],
                "message": message + (repeat if duplicate else "") + "."}

    routes = [] if manager else requests_for_code(storage, user, code)
    event, duplicate = record_tap(storage, user, code, item, signed, now=now, extra=extra or None)
    arrival = arrival_message(storage, item)
    outcome = {**outcome, "ok": True, "event": event, "duplicate": duplicate, "arrival": arrival_kind(item)}
    if routes:
        request = routes[0]
        message = ((arrival + ". " if arrival else "")
                   + f"Toque registrado en tu ruta hacia «{traceability.request_title(request)}»"
                   + (repeat if duplicate else "") + ".")
        return {**outcome, "tone": "success", "target": TARGET_ROUTE, "request_id": request["id"], "message": message}

    message = ((arrival + (repeat if duplicate else "")) if arrival
               else f"Toque registrado: «{name}» ({code})" + (repeat if duplicate else "")) + "."
    pending = [] if manager else open_route_requests(storage, user)
    if pending:
        return {**outcome, "tone": "warning",
                "message": f"{message} {route_mismatch_hint(storage, pending[0], code, item)}"}
    return {**outcome, "tone": "success", "message": message}


# ---------------------------------------------------------------------------
# Registro de chips grabados
# ---------------------------------------------------------------------------

def register_tag(storage, code: str, actor: dict, chip: str = "", now=None):
    """Deja constancia de que se grabo el chip de `code` (devuelve el evento)."""
    code = validate_tag_code(code)
    details = {"chip": chip or "", "signed": bool(nfc_secret())}
    return storage.add_trace_event({
        "event_type": traceability.EVENT_NFC_TAG_WRITTEN, "request_id": "", "item_id": code, "loan_id": "",
        "user_id": (actor or {}).get("id", ""), **_actor_fields(actor), "receipt": "",
        "details": json.dumps(details, ensure_ascii=False), "created_at": _now(now).isoformat(),
    })


def unregister_tag(storage, code: str, actor: dict, reason: str = "", now=None):
    """El chip de `code` se quito, se perdio o se reemplazara."""
    code = str(code or "").strip()
    details = {"reason": " ".join(str(reason or "").split())}
    return storage.add_trace_event({
        "event_type": traceability.EVENT_NFC_TAG_REMOVED, "request_id": "", "item_id": code, "loan_id": "",
        "user_id": (actor or {}).get("id", ""), **_actor_fields(actor), "receipt": "",
        "details": json.dumps(details, ensure_ascii=False), "created_at": _now(now).isoformat(),
    })


def tag_registry(events: list) -> dict:
    """{codigo: estado} a partir de los eventos de chips y toques, en orden de hora.

    Grabado → TAG_WRITTEN; cualquier toque → TAG_TESTED (un toque prueba que el
    chip existe y funciona, aunque nadie lo marcara como grabado); retirado →
    sale del registro hasta que se grabe o se toque otra vez."""
    aware_min = datetime.min.replace(tzinfo=timezone.utc)
    registry = {}
    for event in sorted(events or [], key=lambda row: traceability.to_datetime(row.get("created_at")) or aware_min):
        kind = event.get("event_type")
        code = tap_code(event) if kind == traceability.EVENT_NFC_TAP else str(event.get("item_id") or "")
        if not code:
            continue
        if kind == traceability.EVENT_NFC_TAG_REMOVED:
            registry.pop(code, None)
        elif kind == traceability.EVENT_NFC_TAG_WRITTEN:
            entry = registry.setdefault(code, {"taps": 0, "last_tap_at": "", "last_tap_by": ""})
            entry.update(status=TAG_WRITTEN, written_at=event.get("created_at", ""),
                         written_by=event.get("actor_name", ""),
                         chip=traceability.event_details(event).get("chip", ""))
        elif kind == traceability.EVENT_NFC_TAP:
            entry = registry.setdefault(code, {"taps": 0, "written_at": "", "written_by": "", "chip": ""})
            entry.update(status=TAG_TESTED, last_tap_at=event.get("created_at", ""),
                         last_tap_by=event.get("actor_name", ""))
            entry["taps"] = int(entry.get("taps") or 0) + 1
    return registry


def load_registry(storage) -> dict:
    events = []
    for kind in (traceability.EVENT_NFC_TAG_WRITTEN, traceability.EVENT_NFC_TAG_REMOVED, traceability.EVENT_NFC_TAP):
        events.extend(storage.get_trace_events(event_type=kind))
    return tag_registry(events)


def tag_status_label(entry) -> str:
    return TAG_STATUS_LABELS.get((entry or {}).get("status"), NO_TAG_LABEL)


# ---------------------------------------------------------------------------
# M4: salud de los chips (solo administrador, con nfc_auditoria)
# ---------------------------------------------------------------------------

_AWARE_MIN = datetime.min.replace(tzinfo=timezone.utc)
_ANOMALY_COLUMNS = {ANOMALY_COUNTER: "counter_alerts", ANOMALY_UID: "uid_alerts", ANOMALY_MISSING: "missing_mirror"}


def _count_anomalies(entry: dict, anomalies) -> None:
    for kind in anomalies if isinstance(anomalies, list) else []:
        column = _ANOMALY_COLUMNS.get(kind)
        if column:
            entry[column] += 1


def health_rows(registry: dict, taps: list, rejected: list, now=None, stale_days: int = STALE_DAYS) -> list:
    """Una fila por chip (grabado, tocado o con toques rechazados): toques, último toque, firmas inválidas,
    claves desconocidas, alertas de contador y de UID, UID distintos (chip duplicado), toques bloqueados y si un
    chip registrado lleva ``stale_days`` días o más sin leerse."""
    moment = _now(now)
    rows = {}

    def row(code: str) -> dict:
        return rows.setdefault(code, {
            "code": code, "status": tag_status_label((registry or {}).get(code)), "taps": 0, "last_tap_at": "",
            "invalid_signature": 0, "unknown_key": 0, "counter_alerts": 0, "uid_alerts": 0, "missing_mirror": 0,
            "blocked": 0, "uids": set(), "registered": code in (registry or {}),
        })

    for code in registry or {}:
        row(code)
    for tap in taps or []:
        code = tap_code(tap)
        if not code:
            continue
        entry = row(code)
        entry["taps"] += 1
        stamp = tap.get("created_at") or ""
        if (traceability.to_datetime(stamp) or _AWARE_MIN) > (traceability.to_datetime(entry["last_tap_at"]) or _AWARE_MIN):
            entry["last_tap_at"] = stamp
        details = traceability.event_details(tap)
        mirror = details.get("mirror")
        if isinstance(mirror, dict) and mirror.get("configured") and mirror.get("uid"):
            entry["uids"].add(mirror["uid"])
        _count_anomalies(entry, details.get("anomalies"))
    for event in rejected or []:
        details = traceability.event_details(event)
        code = str(event.get("item_id") or details.get("code") or "")
        if not code:
            continue
        entry = row(code)
        reason = details.get("reason")
        if reason in (SIG_INVALID, SIG_MISSING):
            entry["invalid_signature"] += 1
        elif reason == SIG_UNKNOWN_KEY:
            entry["unknown_key"] += 1
        elif reason == REJECT_ANOMALY:
            entry["blocked"] += 1
            _count_anomalies(entry, details.get("anomalies"))
    result = []
    for entry in rows.values():
        last = traceability.to_datetime(entry["last_tap_at"])
        entry["stale"] = bool(entry["registered"]) and (last is None or (moment - last).days >= stale_days)
        entry["distinct_uids"] = len(entry.pop("uids"))
        entry["duplicated"] = entry["distinct_uids"] > 1 or entry["uid_alerts"] > 0
        result.append(entry)
    return sorted(result, key=lambda entry: inventory_count.code_order(entry["code"]))


def chip_health(storage, now=None, stale_days: int = STALE_DAYS) -> list:
    """``health_rows`` con los datos de la base (solo lectura)."""
    return health_rows(load_registry(storage),
                       storage.get_trace_events(event_type=traceability.EVENT_NFC_TAP),
                       storage.get_trace_events(event_type=traceability.EVENT_NFC_TAP_REJECTED), now, stale_days)
