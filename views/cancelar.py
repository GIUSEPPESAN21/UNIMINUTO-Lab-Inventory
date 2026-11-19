# -*- coding: utf-8 -*-
"""Cancelar un pedido (v1.23): el mismo panel en Despacho y en Solicitudes.

Solo quien tiene ``cancel_order`` (profesor y administrador) ve los botones; :mod:`core.dispatch` lo vuelve a exigir
aunque alguien llame la función directamente. El botón «Cancelar» de una tarjeta guarda en la sesión qué se quiere
cancelar y la página pinta ARRIBA este panel (como «Corregir» en Por entregar): qué cancelar (una línea o todo el
pedido), el motivo obligatorio, la confirmación con la etapa actual y qué pasa con las unidades, y «Sí, cancelar».
"""

import streamlit as st

from core import dispatch, traceability
from core.ui import esc, section_title

FOCUS_DISPATCH = "dispatch_cancel_focus"
FOCUS_REQUESTS = "solicitudes_cancel_focus"
WHOLE_ORDER = "__pedido__"


def open_panel(focus_key: str, request_ids, order_id: str = "", reason: str = "", original: str = "") -> None:
    """Pide el panel de cancelación para ``request_ids`` (o el pedido ``order_id``), con un motivo ya elegido si se
    llega desde el aviso de posible duplicado."""
    st.session_state[focus_key] = {"request_ids": [str(rid) for rid in request_ids or [] if rid],
                                   "order_id": str(order_id or ""), "reason": reason, "original": original}


def cancel_button(container, storage, user: dict, focus_key: str, rows: list, order_id: str, key: str,
                  label: str = "Cancelar") -> None:
    """Botón «Cancelar» de una tarjeta. No se pinta para quien no tiene ``cancel_order``."""
    if not dispatch.can_cancel(user):
        return
    if container.button(label, key=f"cancel_open_{key}", icon=":material/cancel:", use_container_width=True,
                        help="Cancela una línea o todo el pedido, con motivo y confirmación."):
        open_panel(focus_key, [row["id"] for row in rows], order_id)
        st.rerun()


def _line_label(row: dict) -> str:
    return f"{row.get('item_name') or row.get('service_name') or row.get('item_id') or 'Solicitud'} × " \
           f"{int(row.get('quantity') or 0)}"


def _request_option(storage, row: dict) -> str:
    events = storage.get_trace_events(request_id=row["id"])
    stage = traceability.STAGES[traceability.request_stage_key(row, events)][0]
    return f"{traceability.short_id(row['id'])} · {_line_label(row)} · {traceability.fmt_local(row.get('created_at'))} · {stage}"


def _original_picker(storage, lines: list, target: str, key: str, preset: str) -> str:
    """Selector del pedido original (mismo cliente y producto). ``lines``: las líneas que se van a cancelar. Devuelve
    el id del original (o del pedido original) o ``""``."""
    if target == WHOLE_ORDER:
        groups = dispatch.order_duplicate_candidates(storage, lines)
        options = {group["ref"]: group for group in groups}

        def label(ref):
            rows = options[ref]["rows"]
            what = (f"Pedido {ref.upper()} · {len(rows)} producto(s)" if len(rows) > 1 or rows[0].get("order_id")
                    else _line_label(rows[0]))
            return f"{what} · {traceability.fmt_local(rows[0].get('created_at'))}"
    else:
        request = next(row for row in lines if row["id"] == target)
        options = {row["id"]: row for row in dispatch.duplicate_candidates(storage, request)}

        def label(ref):
            return _request_option(storage, options[ref])
    if not options:
        st.warning("No hay otro pedido en pie del mismo cliente y producto: elige otro motivo.",
                   icon=":material/search_off:")
        return ""
    refs = list(options)
    chosen = st.selectbox("Pedido original (el que sigue)", refs, format_func=label,
                          index=refs.index(preset) if preset in refs else 0, key=f"cancel_original_{key}_{target}")
    return chosen or ""


def render_panel(storage, user: dict, focus_key: str, notify) -> None:
    """Panel de cancelación (arriba de la página). ``notify(ok, mensaje)`` deja el aviso para después del rerun."""
    focus = st.session_state.get(focus_key)
    if not focus:
        return
    if not dispatch.can_cancel(user):
        st.session_state.pop(focus_key, None)
        return
    order_id = focus.get("order_id") or ""
    if order_id:
        lines = dispatch.order_lines(storage, order_id)
    else:
        lines = [row for row in (storage.get_service_request(rid) for rid in focus.get("request_ids") or []) if row]
    if not lines:
        st.session_state.pop(focus_key, None)
        return
    key = order_id or lines[0]["id"]
    first = lines[0]
    title = f"Pedido {order_id.upper()} · {len(lines)} productos" if len(lines) > 1 else _line_label(first)
    with st.container(border=True, key=f"cancel_panel_{focus_key}"):
        section_title(f"Cancelar · {title}", icon=":material/cancel:",
                      caption=f"Cliente: {first.get('requester_name') or 'sin nombre'}. Revisa la etapa y qué pasa con "
                              "las unidades antes de confirmar.")
        target = lines[0]["id"]
        if len(lines) > 1:
            options = [row["id"] for row in lines] + [WHOLE_ORDER]
            wanted = (focus.get("request_ids") or [""])[0] if len(focus.get("request_ids") or []) == 1 else WHOLE_ORDER
            target = st.radio(
                "¿Qué cancelas?", options, index=options.index(wanted) if wanted in options else len(options) - 1,
                format_func=lambda rid: "Todo el pedido" if rid == WHOLE_ORDER else
                f"Solo {_line_label(next(row for row in lines if row['id'] == rid))}",
                key=f"cancel_target_{key}")
        targets = [row["id"] for row in lines] if target == WHOLE_ORDER else [target]
        preview = dispatch.cancel_preview(storage, targets)

        reasons = list(dispatch.CANCEL_REASONS)
        preset = focus.get("reason") or None
        reason = st.radio("Motivo (obligatorio)", reasons, format_func=dispatch.CANCEL_REASONS.get, horizontal=True,
                          index=reasons.index(preset) if preset in reasons else None, key=f"cancel_reason_{key}")
        text = st.text_input("Detalle (obligatorio)" if reason == dispatch.CANCEL_OTHER else "Detalle (opcional)",
                             key=f"cancel_text_{key}", placeholder="Lo verá el cliente en Mis pedidos")
        original = ""
        if reason == dispatch.CANCEL_DUPLICATE:
            open_rows = [row["request"] for row in preview if row["can_cancel"]] or [
                row for row in lines if row["id"] in targets]
            original = _original_picker(storage, open_rows, target, key, focus.get("original") or "")

        st.markdown("**Antes de confirmar**")
        st.markdown("\n".join(
            f"- **{esc(_line_label(row['request']))}** · etapa actual: **{esc(row['stage_label'])}** · {esc(row['effect'])}"
            for row in preview))
        cancellable = [row for row in preview if row["can_cancel"]]
        if not cancellable:
            st.info("No hay nada que cancelar aquí. Lo que ya se entregó se recibe en Despacho → Devoluciones.",
                    icon=":material/assignment_return:")
        units = sum(row["units_back"] for row in cancellable)
        if units:
            st.warning(f"Al confirmar, {units} unidad(es) vuelven al estante y el préstamo del picking se cierra.",
                       icon=":material/inventory:")
        ready = (bool(cancellable) and reason is not None
                 and (reason != dispatch.CANCEL_OTHER or bool(" ".join((text or "").split())))
                 and (reason != dispatch.CANCEL_DUPLICATE or bool(original)))
        c1, c2 = st.columns(2)
        if c1.button("Sí, cancelar", key=f"cancel_confirm_{key}", type="primary", icon=":material/cancel:",
                     disabled=not ready, use_container_width=True):
            try:
                if target == WHOLE_ORDER:
                    ok, message, _ = dispatch.cancel_order(storage, order_id, user, reason, text, original)
                else:
                    ok, message, _ = dispatch.cancel_request(storage, target, user, reason, text, original)
            except PermissionError as exc:
                ok, message = False, str(exc)
            notify(ok, message)
            if ok:
                st.session_state.pop(focus_key, None)
            st.rerun()
        if c2.button("No, volver", key=f"cancel_back_{key}", use_container_width=True):
            st.session_state.pop(focus_key, None)
            st.rerun()
