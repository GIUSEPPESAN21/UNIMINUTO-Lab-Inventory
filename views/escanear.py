# -*- coding: utf-8 -*-
"""views/escanear.py - Flujo central: escanear codigo de Contenedor Principal,
Contenedor de Caracteristica o Item Individual; dar salida (checkout) y
registrar reingreso (checkin)."""

import hashlib
from datetime import datetime, timedelta

import streamlit as st

from core import barcode, labels, loans as loans_core, notifications, traceability
from core.labels import ITEM_TYPE_BY_CHOICE, ITEM_TYPE_CHOICES, ITEM_TYPE_HELP, ITEM_TYPE_NAMES
from core.ui import page_header
from views.code_input import render_code_input
from views.location_guide import render_location_guide


def _pickup_label(request: dict, routes: dict) -> str:
    route = routes.get(request["id"])
    proof = (f"ruta verificada · comprobante {traceability.format_receipt(route.get('receipt'))}"
             if route else "sin ruta verificada")
    return f"Solicitud {traceability.short_id(request['id'])} · {request.get('quantity') or 1} u. · {proof}"


def _render_pickup_notice(pickups: list, routes: dict) -> None:
    """Aviso de que quien escanea tiene una solicitud aprobada de este producto."""
    verified = [request for request in pickups if routes.get(request["id"])]
    if verified:
        st.success(
            f":material/explore: Tienes {len(pickups)} solicitud(es) aprobada(s) de este producto; "
            f"{len(verified)} con ruta verificada. Al confirmar la salida quedará enlazada."
        )
    else:
        st.info(
            f":material/explore: Tienes {len(pickups)} solicitud(es) aprobada(s) de este producto. Al confirmar la salida "
            "quedará enlazada; si quieres, primero recorre la ruta verificable en Trazabilidad."
        )


def _render_label_download(item: dict):
    spec = labels.load_label_spec(st.session_state.storage)
    label_pdf, label_png = labels.item_label_files(item, spec)
    if not label_png:
        return
    pdf_col, png_col = st.columns(2)
    pdf_col.download_button(
        f":material/label: PDF {spec.size_text}", data=label_pdf,
        file_name=f"etiqueta_{item['id']}.pdf", mime="application/pdf",
        help="Formato recomendado: mide exactamente lo mismo que la etiqueta",
        key=f"label_scan_pdf_{item['id']}", type="primary", use_container_width=True,
    )
    png_col.download_button(
        "PNG · solo archivo", data=label_png,
        file_name=f"etiqueta_{item['id']}.png", mime="image/png",
        help="Para guardar o integrar. No lo imprimas desde la app Fotos: la recorta y la "
             "agranda. Para imprimir usa el PDF.",
        key=f"label_scan_png_{item['id']}", use_container_width=True,
    )
    st.caption(
        f"Etiqueta de {spec.describe()}: abre el PDF con Edge o Chrome (Ctrl + P), papel USER de "
        f"{spec.size_text}, escala Predeterminado, sin márgenes."
    )

def _render_item_actions(item: dict, parent: dict = None):
    storage = st.session_state.storage
    user = st.session_state.user

    render_location_guide(item, parent, key_prefix=f"scan_guide_{item['id']}")

    if parent:
        st.caption(f":material/shelves: Pertenece al {ITEM_TYPE_NAMES['master']}: **{parent.get('name')}** (`{parent.get('id')}`)")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Disponibles", item["available"])
    c2.metric("Total propiedad del lab", item.get("quantity", 0))
    c3.metric("Ubicacion", item.get("location") or "N/A")
    c4.metric("Categoria", item.get("category") or "N/A")

    if item.get("description"):
        st.caption(item["description"])

    st.markdown("##### :material/outbox: Dar salida (checkout)")
    pickups = traceability.pending_pickups(storage, user, item["id"])
    routes = {request["id"]: traceability.route_event_for(storage, request["id"]) for request in pickups}
    if pickups:
        _render_pickup_notice(pickups, routes)
    if item["available"] <= 0:
        st.warning("No hay unidades disponibles para dar salida en este momento.")
    else:
        with st.form(f"checkout_form_{item['id']}"):
            linked_request = None
            default_qty = 1
            if pickups:
                options = {request["id"]: request for request in pickups}
                choice = st.selectbox(
                    "Solicitud aprobada que retiras", [*options, ""],
                    format_func=lambda rid: _pickup_label(options[rid], routes) if rid else "No vincular con una solicitud",
                    help="La salida queda enlazada a la solicitud en Trazabilidad (evento «retirado»).",
                )
                linked_request = options.get(choice)
                default_qty = int(pickups[0].get("quantity") or 1)
            default_qty = max(1, min(default_qty, int(item["available"])))
            qty = st.number_input("Cantidad", min_value=1, max_value=int(item["available"]), value=default_qty, step=1)
            with_date = st.checkbox("Definir fecha esperada de devolucion")
            expected_date = None
            if with_date:
                expected_date = st.date_input("Devolver antes de", value=datetime.now().date() + timedelta(days=7))
            notes = st.text_input("Notas (opcional)", placeholder="Motivo de uso, practica, proyecto...")
            submitted = st.form_submit_button(":material/check_circle: Confirmar salida", type="primary", use_container_width=True)

            if submitted:
                expected_dt = None
                if with_date:
                    expected_dt = loans_core.deadline_from_date(expected_date)
                ok, msg, loan = loans_core.checkout(storage, item["id"], int(qty), user, expected_dt, notes)
                if ok:
                    st.success(msg)
                    if linked_request:
                        # Se muestra tras el rerun (ver render): la salida ya quedó registrada.
                        linked, link_msg, _ = traceability.record_pickup(storage, linked_request, loan, user)
                        st.session_state.scan_trace_notice = (linked, f"{msg} {link_msg}")
                    notifications.send_whatsapp_alert_async(f"📤 Salida: {msg}")
                    st.session_state.scan_result = barcode.scan(storage, item["id"])
                    st.rerun()
                else:
                    st.error(msg)

    st.markdown("##### :material/move_to_inbox: Reingresar (checkin)")
    open_loans = storage.get_open_loans_for_item(item["id"])
    if user["role"] == "estudiante":
        open_loans = [l for l in open_loans if l["user_id"] == user["id"]]

    if not open_loans:
        st.info("No hay prestamos abiertos de este item para reingresar.")
    else:
        for loan in open_loans:
            overdue_tag = " · :red[:material/warning: **VENCIDO**]" if loans_core.is_overdue(loan) else ""
            with st.container(border=True):
                cc1, cc2 = st.columns([3, 1])
                cc1.write(f"**{loan['user_name']}** ({loan['user_role']}) · {loan['quantity']} u.{overdue_tag}")
                if cc2.button("Reingresar", key=f"checkin_{loan['id']}", use_container_width=True):
                    ok, msg = loans_core.checkin(storage, loan["id"], user)
                    if ok:
                        st.success(msg)
                        st.session_state.scan_result = barcode.scan(storage, item["id"])
                        st.rerun()
                    else:
                        st.error(msg)


def _render_new_item_wizard(scanned_code: str):
    storage = st.session_state.storage
    user = st.session_state.user

    st.warning(f"El codigo `{scanned_code}` no existe todavia en el inventario.")
    if not barcode.is_valid_code(scanned_code):
        st.caption(f":material/warning: Este codigo no cumple ningun formato valido. {barcode.FORMAT_HELP}")

    if user["role"] == "estudiante":
        st.info("Pide a un profesor o al administrador del laboratorio que registre este item.")
        return

    item_kind = st.radio("¿Qué quieres registrar?", ITEM_TYPE_CHOICES, horizontal=True)
    item_type = ITEM_TYPE_BY_CHOICE[item_kind]
    st.caption(ITEM_TYPE_HELP[item_type])

    # El hash solo forma una clave de widget estable: evita conservar valores de
    # un codigo escaneado anterior cuando cambia el resultado de busqueda.
    code_token = hashlib.sha1(scanned_code.encode("utf-8")).hexdigest()[:10]
    code_to_save = render_code_input(
        storage, item_type, key_prefix=f"scan_new_code_{code_token}", initial_code=scanned_code
    )

    with st.form("new_item_form"):
        name_label = "Nombre / Característica" if item_kind == ITEM_TYPE_NAMES["child"] else "Nombre"
        name_placeholder = "Ej: Resistencias 220 Ω" if item_kind == ITEM_TYPE_NAMES["child"] else None
        name = st.text_input(name_label, placeholder=name_placeholder)
        category = st.text_input("Categoria", placeholder="Electronica, Mecanica, EPP, Herramientas...")
        description = st.text_area("Descripcion", height=80)
        location = st.text_input("Ubicacion fisica", placeholder="Estante 3, Gabinete B...")

        parent_id = ""
        if item_kind == ITEM_TYPE_NAMES["child"]:
            masters = storage.get_all_masters()
            options = {f"{m['name']} ({m['id']})": m["id"] for m in masters}
            if not options:
                st.warning(f"Todavia no hay {ITEM_TYPE_NAMES['master'].lower()}es creados.")
            else:
                choice = st.selectbox(ITEM_TYPE_NAMES["master"], list(options.keys()))
                parent_id = options.get(choice, "")

        quantity, min_alert = 0, 0
        if item_kind != ITEM_TYPE_NAMES["master"]:
            quantity = st.number_input("Cantidad inicial", min_value=0, step=1, value=1)
            min_alert = st.number_input("Umbral de alerta de disponibilidad", min_value=0, step=1, value=0)

        submitted = st.form_submit_button(":material/save: Registrar item", type="primary", use_container_width=True)

        if submitted:
            code_to_save = (code_to_save or "").strip()
            code_error = None
            if code_to_save:
                try:
                    barcode.validate_code_format(code_to_save)
                except ValueError as exc:
                    code_error = str(exc)

            if not code_to_save:
                st.error("El código es obligatorio.")
            elif code_error:
                st.error(code_error)
            elif storage.item_code_in_use(code_to_save):
                st.error("Ya existe un item con ese código.")
            elif not name:
                st.error("El nombre es obligatorio.")
            elif item_kind == ITEM_TYPE_NAMES["child"] and not parent_id:
                st.error(f"Debes seleccionar un {ITEM_TYPE_NAMES['master']}.")
            else:
                data = {
                    "name": name, "category": category, "description": description,
                    "item_type": item_type, "parent_id": parent_id, "unit": "unidad",
                    "quantity": int(quantity), "location": location,
                    "min_stock_alert": int(min_alert), "status": "active",
                    "created_by": user["institutional_email"],
                }
                try:
                    storage.save_item(data, code_to_save, is_new=True,
                                      actor_email=user["institutional_email"])
                    st.success(f"¡'{name}' registrado correctamente con el código {code_to_save}!")
                    st.session_state.scan_result = barcode.scan(storage, code_to_save)
                    st.rerun()
                except Exception as e:
                    st.error(f"Error al registrar: {e}")


def render():
    storage = st.session_state.storage

    page_header("Escanear", icon=":material/barcode_scanner:", subtitle="Conecta tu lector USB o escribe el código manualmente")

    with st.form("scan_form", clear_on_submit=True):
        code = st.text_input("Codigo de barras", placeholder="Escanea aqui...")
        st.caption(barcode.FORMAT_HELP)
        submitted = st.form_submit_button("Buscar", use_container_width=True)
        if submitted and code:
            st.session_state.scan_result = barcode.scan(storage, code)
        elif submitted and not code:
            st.warning("Ingresa o escanea un codigo.")

    result = st.session_state.get("scan_result")
    st.markdown("---")

    notice = st.session_state.pop("scan_trace_notice", None)
    if notice:
        linked, message = notice
        (st.success if linked else st.warning)(f":material/explore: {message}")

    if not result:
        st.caption("Esperando escaneo...")
        return

    if result["status"] == "error":
        st.error(result["message"])
        if result.get("retired"):
            st.caption("Su código quedó libre: puedes registrarlo de nuevo como un item nuevo.")
            _render_new_item_wizard(result["barcode"])
    elif result["status"] == "not_found":
        _render_new_item_wizard(result["barcode"])
    elif result["status"] == "found_master":
        item = result["item"]
        st.success(f":material/shelves: {ITEM_TYPE_NAMES['master']}: **{item['name']}** (`{item['id']}`)")
        if result.get("parsed"):
            st.caption(f":material/menu_book: {barcode.describe_parsed(result['parsed'])}")
        if item.get("description"):
            st.caption(item["description"])
        st.caption(f"Ubicacion: {item.get('location') or 'N/A'}")
        _render_label_download(item)
        render_location_guide(item, key_prefix=f"scan_master_guide_{item['id']}")
        children = result["children"]
        if not children:
            st.info(f"Este {ITEM_TYPE_NAMES['master']} todavía no tiene {ITEM_TYPE_NAMES['child']}s registrados dentro.")
        else:
            for child in children:
                with st.expander(f"{child['name']} — {child['available']} disponibles"):
                    _render_item_actions(child, parent=item)
    elif result["status"] == "found_item":
        item = result["item"]
        st.success(f":material/check: Item encontrado: **{item['name']}** (`{item['id']}`)")
        if result.get("parsed"):
            st.caption(f":material/menu_book: {barcode.describe_parsed(result['parsed'])}")
        _render_label_download(item)
        _render_item_actions(item, parent=result.get("parent"))
