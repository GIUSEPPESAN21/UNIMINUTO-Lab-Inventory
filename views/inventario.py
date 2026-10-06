# -*- coding: utf-8 -*-
"""views/inventario.py - Catalogo, alta/edicion/eliminacion de items y contenedores.
Solo profesor/maestro pueden crear, editar o eliminar."""

from collections import Counter

import pandas as pd
import streamlit as st

from core import barcode
from core import labels
from core import permissions
from core.labels import ITEM_TYPE_BY_CHOICE, ITEM_TYPE_CHOICES, ITEM_TYPE_HELP, ITEM_TYPE_LABELS, ITEM_TYPE_NAMES
from core.ui import esc, guard_role, page_header
from views.code_input import render_code_input


def _delete_warning(impact: dict) -> str:
    """Texto de advertencia previo a eliminar (lo que se borrara de verdad)."""
    parts = [
        "Eliminar es **permanente**: se borran el item, su historial y sus préstamos ya devueltos, "
        "y su código queda libre para registrarse de nuevo."
    ]
    if impact.get("contained_items"):
        parts.append(
            f"Al ser un {ITEM_TYPE_NAMES['master']}, también se eliminarán los "
            f"{impact['contained_items']} item(s) que contiene."
        )
    if impact.get("open_requests"):
        parts.append(
            f"Se cancelarán {impact['open_requests']} solicitud(es) pendiente(s) o aprobada(s) de este producto."
        )
    if impact.get("open_loans"):
        parts.append(
            f"⚠️ Hay {impact['open_loans']} préstamo(s) abierto(s): registra primero su reingreso; "
            "mientras tanto no se podrá eliminar."
        )
    return " ".join(parts)


def _edit_item_form(storage, item: dict, user: dict):
    st.markdown(f'<h2 style="text-align:center;">✏️ Editando: {esc(item.get("name"))}</h2>', unsafe_allow_html=True)
    impact = storage.get_delete_impact(item["id"])
    with st.form("edit_item_form"):
        name = st.text_input("Nombre", value=item.get("name", ""))
        category = st.text_input("Categoria", value=item.get("category", ""))
        description = st.text_area("Descripcion", value=item.get("description", ""))
        location = st.text_input("Ubicacion", value=item.get("location", ""))

        quantity, min_alert = item.get("quantity", 0), item.get("min_stock_alert", 0)
        if item.get("item_type") != "master":
            quantity = st.number_input("Cantidad total", value=int(item.get("quantity", 0)), min_value=0, step=1)
            min_alert = st.number_input("Umbral de alerta", value=int(item.get("min_stock_alert", 0)), min_value=0, step=1)

        st.markdown("---")
        st.caption(_delete_warning(impact))
        confirm_delete = st.checkbox("Entiendo que se eliminará de forma permanente", key=f"confirm_delete_{item['id']}")
        c1, c2, c3 = st.columns(3)
        save = c1.form_submit_button("Guardar cambios", type="primary", use_container_width=True)
        delete = c2.form_submit_button("🗑️ Eliminar definitivamente", use_container_width=True)
        cancel = c3.form_submit_button("Cancelar", use_container_width=True)

        if save:
            if not name:
                st.warning("El nombre no puede estar vacio.")
            else:
                data = {
                    **item, "name": name, "category": category, "description": description,
                    "location": location, "quantity": int(quantity), "min_stock_alert": int(min_alert),
                }
                try:
                    storage.save_item(data, item["id"], is_new=False, actor_email=user["institutional_email"])
                    st.success("Item actualizado.")
                    st.session_state.editing_item_id = None
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))

        if delete:
            if not confirm_delete:
                st.warning("Marca la casilla de confirmación para eliminar el item de forma permanente.")
            else:
                ok, msg = storage.delete_item(item["id"], actor_email=user["institutional_email"])
                if ok:
                    st.toast(msg, icon="🗑️")
                    st.session_state.editing_item_id = None
                    st.session_state.scan_result = None  # evita mostrar el item recien eliminado
                    st.rerun()
                else:
                    st.error(msg)

        if cancel:
            st.session_state.editing_item_id = None
            st.rerun()


def render():
    storage = st.session_state.storage
    user = st.session_state.user

    if not guard_role(user, permissions.MANAGER_ROLES, "el Inventario"):
        return

    page_header("Inventario", icon="📦", subtitle="Catálogo, alta, edición e importación masiva")

    if st.session_state.get("editing_item_id"):
        item = storage.get_item(st.session_state.editing_item_id)
        if not item:
            st.error("El item ya no existe.")
            st.session_state.editing_item_id = None
        else:
            _edit_item_form(storage, item, user)
        return

    tab_catalogo, tab_nuevo, tab_import = st.tabs(
        ["📋 Catalogo", "➕ Nuevo item", "📥 Importar CSV masivo"]
    )

    with tab_catalogo:
        try:
            items = storage.get_all_items()
        except Exception as e:
            st.error(f"Error al cargar el inventario: {e}")
            items = []

        availability = storage.get_availability_map() if items else {}
        children_count = Counter(i.get("parent_id") for i in items if i.get("parent_id"))

        f1, f2, f3, f4 = st.columns(4)
        search = f1.text_input("Buscar por nombre, categoria o codigo")
        categories = sorted({i.get("category") for i in items if i.get("category")})
        locations = sorted({i.get("location") for i in items if i.get("location")})
        cat_filter = f2.selectbox("Categoria", ["Todas"] + categories)
        loc_filter = f3.selectbox("Ubicacion", ["Todas"] + locations)
        type_filter = f4.selectbox("Tipo", ["Todos"] + list(ITEM_TYPE_LABELS.values()))

        if search:
            s = search.lower()
            items = [
                i for i in items
                if s in (i.get("name") or "").lower()
                or s in (i.get("category") or "").lower()
                or s in (i.get("id") or "").lower()
            ]
        if cat_filter != "Todas":
            items = [i for i in items if i.get("category") == cat_filter]
        if loc_filter != "Todas":
            items = [i for i in items if i.get("location") == loc_filter]
        if type_filter != "Todos":
            items = [i for i in items if ITEM_TYPE_LABELS.get(i.get("item_type")) == type_filter]

        st.caption(f"{len(items)} item(s) encontrados.")
        if not items:
            st.info("No se encontraron items.")
        for item in items:
            with st.container(border=True):
                c1, c2, c3, c4, c5 = st.columns([4, 2, 2, 1, 1])
                tag = ITEM_TYPE_LABELS.get(item.get("item_type"), item.get("item_type"))
                c1.markdown(f"**{item.get('name')}**")
                c1.caption(f"{tag} · ID: {item.get('id')} · {item.get('category') or 'Sin categoria'}")
                if item.get("item_type") == "master":
                    c2.metric("Items dentro", children_count.get(item["id"], 0))
                else:
                    c2.metric("Disponibles", availability.get(item["id"], 0))
                c3.metric("Ubicacion", item.get("location") or "N/A")
                if c4.button("✏️", key=f"edit_{item['id']}", help="Editar"):
                    st.session_state.editing_item_id = item["id"]
                    st.rerun()
                label_png = labels.generate_item_label_png_bytes(item)
                if label_png:
                    label_pdf = labels.generate_item_label_pdf_bytes(item)
                    with c5.popover("🏷️", help="Descargar etiqueta profesional", use_container_width=True):
                        if label_pdf:
                            st.download_button(
                                "PDF · imprimir", data=label_pdf,
                                file_name=f"etiqueta_{item['id']}.pdf", mime="application/pdf",
                                key=f"label_pdf_{item['id']}", type="primary", use_container_width=True,
                            )
                        st.download_button(
                            "PNG · respaldo", data=label_png,
                            file_name=f"etiqueta_{item['id']}.png", mime="image/png",
                            key=f"label_png_{item['id']}", use_container_width=True,
                        )
                        st.caption("PDF recomendado: papel 50×25 mm, horizontal, escala 100 % y sin márgenes.")
                else:
                    c5.write("")

    with tab_nuevo:
        st.caption("También puedes registrar un item nuevo directamente escaneando su código en la sección Escanear.")
        item_kind = st.radio("¿Qué quieres registrar?", ITEM_TYPE_CHOICES, horizontal=True, key="inv_new_kind")
        item_type = ITEM_TYPE_BY_CHOICE[item_kind]
        st.caption(ITEM_TYPE_HELP[item_type])

        # Fuera del formulario para que formato, numeracion y vista previa se
        # actualicen inmediatamente en cada interaccion.
        new_id = render_code_input(storage, item_type, key_prefix="inv_new_code")

        with st.form("inv_new_item_form"):
            name_label = "Nombre / Característica" if item_kind == ITEM_TYPE_NAMES["child"] else "Nombre"
            name_placeholder = "Ej: Resistencias 220 Ω" if item_kind == ITEM_TYPE_NAMES["child"] else None
            name = st.text_input(name_label, placeholder=name_placeholder)
            category = st.text_input("Categoria")
            description = st.text_area("Descripcion", height=80)
            location = st.text_input("Ubicacion fisica")

            parent_id = ""
            if item_kind == ITEM_TYPE_NAMES["child"]:
                masters = storage.get_all_masters()
                options = {f"{m['name']} ({m['id']})": m["id"] for m in masters}
                if options:
                    choice = st.selectbox(ITEM_TYPE_NAMES["master"], list(options.keys()))
                    parent_id = options.get(choice, "")
                else:
                    st.warning(f"Todavia no hay {ITEM_TYPE_NAMES['master'].lower()}es creados.")

            quantity, min_alert = 0, 0
            if item_kind != ITEM_TYPE_NAMES["master"]:
                quantity = st.number_input("Cantidad inicial", min_value=0, step=1, value=1, key="inv_new_qty")
                min_alert = st.number_input("Umbral de alerta", min_value=0, step=1, value=0, key="inv_new_alert")

            submitted = st.form_submit_button("💾 Registrar", type="primary", use_container_width=True)

            if submitted:
                new_id = (new_id or "").strip()
                code_error = None
                if new_id:
                    try:
                        barcode.validate_code_format(new_id)
                    except ValueError as e:
                        code_error = str(e)

                if not new_id or not name:
                    st.error("Codigo de barras y nombre son obligatorios.")
                elif code_error:
                    st.error(code_error)
                elif storage.item_code_in_use(new_id):
                    st.error("Ya existe un item con ese codigo.")
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
                        storage.save_item(data, new_id, is_new=True, actor_email=user["institutional_email"])
                        st.success(f"'{name}' registrado correctamente con el código {new_id}.")
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))

    with tab_import:
        st.caption(
            "Sube un CSV para registrar o actualizar muchos items de una sola vez "
            "(ideal para cargar el inventario inicial del laboratorio)."
        )
        st.caption(
            "Usa 'master' para un Contenedor Principal, 'child' para un Contenedor de "
            "Característica (con su 'parent_id' apuntando al maestro) y 'standalone' "
            "para un Ítem Individual en la columna item_type."
        )
        st.caption(barcode.FORMAT_HELP)
        st.caption(
            "La columna id se lee como texto, asi que los ceros a la izquierda (ej. 0012345) se "
            "conservan. Si editas el CSV en Excel, formatea esa columna como Texto antes de "
            "guardarlo: Excel los borra."
        )
        template_csv = (
            "id,name,category,description,item_type,parent_id,unit,quantity,location,min_stock_alert\n"
            "1-2-05-00-000,Caja de Electronica,Electronica,Contenedor principal de componentes,master,,unidad,0,Estante 3,0\n"
            "1-2-05-12-000,Resistencias 220 ohm,Electronica,Paquete de 10,child,1-2-05-00-000,paquete,20,Estante 3,5\n"
            "M1-E1,Multimetro digital,Instrumentacion,Fluke 115,standalone,,unidad,4,Mesa 1,1\n"
        )
        st.download_button(
            "⬇️ Descargar plantilla CSV", data=template_csv, file_name="plantilla_items_laboratorio.csv",
            mime="text/csv", use_container_width=True,
        )

        uploaded = st.file_uploader("Archivo CSV", type=["csv"])
        if uploaded is not None:
            try:
                df_upload = pd.read_csv(uploaded, dtype=str).fillna("")
                st.dataframe(df_upload, use_container_width=True, hide_index=True)

                if st.button("📤 Procesar importacion", type="primary", use_container_width=True):
                    rows = df_upload.to_dict(orient="records")
                    with st.spinner("Importando items..."):
                        result = storage.bulk_upsert_items(rows, actor_email=user["institutional_email"])
                    st.success(
                        f"Importacion completada: {len(result['created'])} creados, "
                        f"{len(result['updated'])} actualizados."
                    )
                    if result["errors"]:
                        st.warning("Algunas filas no se importaron:")
                        for err in result["errors"]:
                            st.caption(f"⚠️ {err}")
                    st.rerun()
            except Exception as e:
                st.error(f"No se pudo leer el CSV: {e}")
