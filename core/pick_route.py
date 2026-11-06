# -*- coding: utf-8 -*-
"""core/pick_route.py - Ruta obligatoria del picking: estantería → piso → contenedor → casilla de la caja.

Antes de sacar las unidades de un pedido, quien hace el picking tiene que llegar leyendo, EN ORDEN, cada punto con
etiqueta del camino (la misma ruta que ve el cliente): primero la estantería, luego su piso y después el contenedor.
Esos tres llevan chip NFC (también sirve su código de barras). **La caja no se lee**: es un paso de casilla
obligatorio («confirmo que abrí la Caja 02 y saqué las unidades»), que deja registrado quién retiró. Una lectura fuera
de orden o de otro lugar no avanza: queda como lectura rechazada con la indicación de qué leer.

Estantería y piso solo se exigen si esa ubicación está registrada (Inventario → Ubicaciones), igual que en la ruta
del cliente. El administrador puede apagar la exigencia en Despacho → Ajustes."""

from core import location, permissions

SETTING_ROUTE_REQUIRED = "picking_route_required"
_OFF = {"0", "no", "false", "off", "apagado", "n"}

# v1.23 (apagada por omision): hoy la caja se confirma con una casilla ("confirmo que
# abri la Caja XX...") sin leer su codigo, lo que no distingue abrir la caja correcta de
# otra del mismo contenedor. Activada, la caja se vuelve un punto mas de la ruta: se lee
# como estanteria/piso/contenedor y una lectura que no coincide se rechaza (ver checkpoints()).
SETTING_BOX_READ_REQUIRED = "exigir_lectura_caja"
_BOX_READ_ON = {"1", "si", "sí", "true", "yes", "on", "activado"}


def required(storage) -> bool:
    """¿El picking exige leer la ruta? Sí por omisión; un ajuste ilegible nunca frena el picking."""
    try:
        value = str(storage.get_setting(SETTING_ROUTE_REQUIRED, "1") or "").strip().lower()
    except Exception:
        value = "1"
    return value not in _OFF


def set_required(storage, actor: dict, on: bool):
    permissions.require_cap(actor, permissions.CAP_DISPATCH_SETTINGS,
                            "Solo un administrador cambia la ruta obligatoria del picking.")
    storage.set_setting(SETTING_ROUTE_REQUIRED, "1" if on else "0", (actor or {}).get("institutional_email", ""))
    return True, ("Ruta obligatoria: el picking exige leer estantería, piso, contenedor y caja."
                  if on else "Ruta obligatoria apagada: el picking solo lee las unidades.")


def box_read_required(storage) -> bool:
    """¿La caja se lee (barras o NFC) en vez de confirmarse con una casilla? No por omisión."""
    try:
        value = str(storage.get_setting(SETTING_BOX_READ_REQUIRED, "0") or "").strip().lower()
    except Exception:
        value = "0"
    return value in _BOX_READ_ON


def enforced(storage) -> bool:
    """¿Hay algo de la ruta que leer? La ruta obligatoria o, aunque esté apagada, la lectura de la caja."""
    return required(storage) or box_read_required(storage)


def box_read_applies(storage, item_id: str) -> bool:
    """True si la caja de ``item_id`` se debe LEER: hay caja, tiene etiqueta y ``exigir_lectura_caja`` está activo.
    Una caja sin etiqueta sigue con su casilla."""
    if not box_read_required(storage):
        return False
    box = box_step(storage, item_id)
    return bool(box and box.get("label_code"))


def set_box_read_required(storage, actor: dict, on: bool):
    permissions.require_cap(actor, permissions.CAP_DISPATCH_SETTINGS,
                            "Solo un administrador exige la lectura de la caja.")
    storage.set_setting(SETTING_BOX_READ_REQUIRED, "1" if on else "0", (actor or {}).get("institutional_email", ""))
    return True, ("La caja ahora se lee (barras o NFC): una lectura que no coincida con la caja esperada se rechaza."
                  if on else "La caja vuelve a confirmarse con una casilla, sin leer su código.")


def _registered_places(storage, item: dict, parent: dict) -> dict:
    found = {}
    for code in location.place_codes(item, parent):
        try:
            place = storage.get_item(code)
        except Exception:
            place = None
        if place and place.get("item_type") == "location" and place.get("status") != "retired":
            found[code] = place.get("name") or ""
    return found


def _route(storage, item_id: str) -> list:
    """Todos los puntos de la ruta hacia el producto (el último es el propio producto)."""
    item = storage.get_item(item_id)
    if not item:
        return []
    parent = storage.get_item(item.get("parent_id")) if item.get("parent_id") else None
    places = _registered_places(storage, item, parent)
    return location.build_route_checkpoints(item, parent, places, places)


def checkpoints(storage, item_id: str) -> list:
    """Puntos con etiqueta que hay que LEER antes de sacar las unidades, en orden: estantería, piso, contenedor y,
    con ``exigir_lectura_caja`` activo, también la caja. Por omisión la caja no se lee (ver ``box_step``) y el
    último punto es el propio producto. Vacío si el producto no tiene una ruta estructurada (códigos libres) o si
    no hay etiqueta que leer."""
    points = _route(storage, item_id)[:-1]
    if box_read_required(storage):
        if not required(storage):     # ruta apagada: la caja se sigue leyendo (reemplaza su casilla)
            return [point for point in points if point.get("key") == "caja" and point.get("label_code")]
        return [point for point in points if point.get("label_code")]
    return [point for point in points if point.get("label_code") and point.get("key") != "caja"]


def box_step(storage, item_id: str):
    """La caja de la ruta (``{"key", "title", "label_code", …}``) o ``None`` si el producto no está en una caja.
    Se confirma con una casilla —obligatoria— y no leyendo su código."""
    return next((point for point in _route(storage, item_id) if point.get("key") == "caja"), None)


def progress(storage, item_id: str, steps: list) -> dict:
    """Avance de la ruta: ``{"points", "done", "next", "complete"}`` según los pasos ya confirmados."""
    points = checkpoints(storage, item_id)
    done = min(len(steps or []), len(points))
    nxt = points[done] if done < len(points) else None
    return {"points": points, "done": done, "next": nxt, "complete": nxt is None}


def hint(point: dict) -> str:
    return f"{point['title']} ({point['label_code']})"


def read(storage, request: dict, steps: list, code: str):
    """Interpreta una lectura mientras falte ruta. ``None`` si no hay nada que exigir (la lectura sigue su
    camino normal); si no, ``(ok, mensaje, punto)``: ``punto`` es el confirmado (o ``None``)."""
    state = progress(storage, request.get("item_id"), steps)
    if state["complete"]:
        if any(location.codes_match(point["label_code"], code) for point in state["points"]):
            return True, "Ese punto de la ruta ya estaba confirmado.", None
        box = box_step(storage, request.get("item_id"))
        if (box and box.get("label_code") and location.codes_match(box["label_code"], code)
                and not location.codes_match(request.get("item_id"), code)):
            return True, f"La {box['title']} no se lee: marca la casilla de confirmación.", None
        return None
    nxt = state["next"]
    if location.codes_match(nxt["label_code"], code):
        after = state["points"][state["done"] + 1] if state["done"] + 1 < len(state["points"]) else None
        follow = f"Sigue: {hint(after)}." if after else f"Ruta completa. {next_step_text(storage, request)}"
        return True, f"Llegaste a {nxt['title']} · {state['done'] + 1}/{len(state['points'])}. {follow}", nxt
    return False, f"Primero confirma la ruta: lee {hint(nxt)}. Se leyó {code}.", None


def next_step_text(storage, request: dict) -> str:
    """Qué sigue cuando la ruta está completa."""
    box = box_step(storage, (request or {}).get("item_id"))
    if box and box_read_required(storage):
        return f"Ya leíste la {box['title']}: ahora saca las unidades y confírmalas."
    if box:
        return f"Ahora abre la {box['title']}, saca las unidades y marca la casilla de confirmación."
    return "Ahora lee la etiqueta del producto, saca las unidades y confírmalas."
