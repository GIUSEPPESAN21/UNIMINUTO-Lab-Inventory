# -*- coding: utf-8 -*-
"""core/pick_route.py - Ruta obligatoria del picking: estantería → piso → contenedor → caja → unidades.

Antes de leer las unidades de un pedido, quien hace el picking tiene que llegar leyendo, EN ORDEN, cada
punto con etiqueta del camino (la misma ruta que ve el cliente): primero la estantería, luego su piso, después
el contenedor y por último la caja. Las estanterías, los pisos y los contenedores llevan chip NFC (también sirve
su código de barras); las cajas y los productos, código de barras. Una lectura fuera de orden o de otro lugar no
avanza: queda como lectura rechazada con la indicación de qué leer.

Estantería y piso solo se exigen si esa ubicación está registrada (Inventario → Ubicaciones), igual que en la ruta
del cliente. El administrador puede apagar la exigencia en Despacho → Ajustes."""

from core import location, permissions

SETTING_ROUTE_REQUIRED = "picking_route_required"
_OFF = {"0", "no", "false", "off", "apagado", "n"}


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


def checkpoints(storage, item_id: str) -> list:
    """Puntos con etiqueta que hay que leer antes de las unidades, en orden. Vacío si el producto no tiene
    una ruta estructurada (códigos libres) o si no hay etiqueta que leer."""
    item = storage.get_item(item_id)
    if not item:
        return []
    parent = storage.get_item(item.get("parent_id")) if item.get("parent_id") else None
    places = _registered_places(storage, item, parent)
    points = location.build_route_checkpoints(item, parent, places, places)
    # El último punto es el propio producto: sus unidades se leen después, una a una.
    return [point for point in points[:-1] if point.get("label_code")]


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
    camino normal de unidades); si no, ``(ok, mensaje, punto)``: ``punto`` es el confirmado (o ``None``)."""
    state = progress(storage, request.get("item_id"), steps)
    if state["complete"]:
        if any(location.codes_match(point["label_code"], code) for point in state["points"]):
            return True, "Ese punto de la ruta ya estaba confirmado.", None
        return None
    nxt = state["next"]
    if location.codes_match(nxt["label_code"], code):
        after = state["points"][state["done"] + 1] if state["done"] + 1 < len(state["points"]) else None
        follow = (f"Sigue: {hint(after)}." if after
                  else "Ruta completa: ahora lee las unidades del producto.")
        return True, f"Llegaste a {nxt['title']} · {state['done'] + 1}/{len(state['points'])}. {follow}", nxt
    return False, f"Primero confirma la ruta: lee {hint(nxt)}. Se leyó {code}.", None
