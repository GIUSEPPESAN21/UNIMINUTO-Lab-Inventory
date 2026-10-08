# -*- coding: utf-8 -*-
"""views/label_settings.py - Tamaño real de la etiqueta, contenido, vista previa
y hoja de prueba de impresion. La configuracion se guarda en la base y la usan
todas las etiquetas de la app (Inventario, Escanear y registro de items)."""

import io

import streamlit as st

from core import labels
from core.barcode import is_valid_code
from core.labels import LABEL_CONTENT_OPTIONS, LABEL_SIZE_PRESETS, LabelSpec

_CUSTOM = "custom"
_SAMPLE_ITEM = {
    "id": "2-1-01-01-001", "name": "Multímetro digital", "item_type": "standalone",
    "category": "Instrumentación", "location": "Laboratorio 3",
}

PRINT_GUIDE = """
1. **Mide tu etiqueta física** (ancho × alto, sin el papel de soporte) y elige ese
   tamaño arriba. Guarda la configuración.
2. **En el driver de la SAT TT460** (*Preferencias de impresión → Papel / Stock*)
   usa un tamaño de papel **igual al de la etiqueta** (por ejemplo, 50 × 25 mm).
3. **Abre el PDF e imprime sin escalar:**
   - **Chrome o Edge:** *Más opciones de configuración → Escala:* **Predeterminado**
     (o **Tamaño real**, si aparece). No uses *Ajustar al área de impresión* ni
     *Ajustar al papel*. El navegador recuerda la última escala: revísala una vez.
   - **Adobe Acrobat Reader:** *Tamaño y gestión de páginas →* **Tamaño real**, y marca
     *Elegir origen del papel por tamaño de página del PDF*.
   - Orientación: la que muestre la etiqueta **completa y sin girar** en la vista previa.
4. **Imprime la hoja de prueba y mide la regla con una regla real:**
   - Mide lo indicado y el marco se ve completo: la impresión es a tamaño real.
   - **La regla mide menos** (por ejemplo, 20 mm en vez de 40 mm): el programa está
     reduciendo la página. Revisa la escala (100 %) y el tamaño de papel del driver.
   - **La regla mide bien, pero la prueba ocupa solo una parte de tu etiqueta:** tu
     rollo es más grande. Mide la etiqueta y elige ese tamaño aquí.
   - **El marco sale cortado o corrido:** el papel del driver no coincide con el rollo,
     o falta calibrar el sensor de etiquetas (consulta el manual; en muchos modelos se
     hace manteniendo presionado el botón FEED).
5. El **PNG es solo un respaldo**: los navegadores ignoran su resolución y suelen
   imprimirlo a otro tamaño. Para imprimir, usa siempre el **PDF**.
"""


def _decimal(value: float, digits: int) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


def _preset_name(key: str) -> str:
    if key == _CUSTOM:
        return "Personalizado…"
    width, height = LABEL_SIZE_PRESETS[key]
    suffix = " (4 × 6 in)" if key == "100x150" else ""
    return f"{width} × {height} mm{suffix}"


def _preview_items(storage) -> dict:
    options = {"Ejemplo: Multímetro digital (2-1-01-01-001)": _SAMPLE_ITEM}
    try:
        items = storage.get_all_items()
    except Exception:
        items = []
    for item in items:
        if is_valid_code(item.get("id") or ""):
            options[f"{item.get('name')} ({item.get('id')})"] = item
    return options


def _spec_from_inputs(saved: LabelSpec):
    """Lee los controles y devuelve (spec, error)."""
    keys = list(LABEL_SIZE_PRESETS) + [_CUSTOM]
    size_col, dpi_col = st.columns([3, 2])
    preset = size_col.selectbox(
        "Tamaño del rollo de etiquetas", keys, index=keys.index(saved.preset_key),
        format_func=_preset_name, key="label_cfg_preset",
    )
    dpi = dpi_col.radio(
        "Resolución de la impresora", labels.SUPPORTED_DPI,
        index=labels.SUPPORTED_DPI.index(saved.dpi), horizontal=True,
        format_func=lambda value: f"{value} dpi" + (" · SAT TT460" if value == labels.LABEL_DPI else ""),
        key="label_cfg_dpi",
    )
    if preset == _CUSTOM:
        width_col, height_col = st.columns(2)
        width = width_col.number_input(
            "Ancho (mm)", min_value=float(labels.MIN_LABEL_WIDTH_MM),
            max_value=float(labels.MAX_LABEL_WIDTH_MM), value=float(saved.width_mm),
            step=1.0, key="label_cfg_width",
        )
        height = height_col.number_input(
            "Alto (mm)", min_value=float(labels.MIN_LABEL_HEIGHT_MM),
            max_value=float(labels.MAX_LABEL_HEIGHT_MM), value=float(saved.height_mm),
            step=1.0, key="label_cfg_height",
        )
    else:
        width, height = LABEL_SIZE_PRESETS[preset]

    st.markdown("**Contenido opcional**")
    columns = st.columns(3)
    chosen = [
        key for index, (key, text) in enumerate(LABEL_CONTENT_OPTIONS.items())
        if columns[index % 3].checkbox(text, value=saved.shows(key), key=f"label_cfg_{key}")
    ]
    st.caption(
        "El nombre, el código de barras y el código legible siempre se imprimen. Si algo no "
        "cabe a un tamaño legible, se omite automáticamente en lugar de apretar las letras."
    )
    try:
        return LabelSpec(width, height, dpi, tuple(chosen)), None
    except ValueError as exc:
        return None, str(exc)


def _render_preview(storage, spec: LabelSpec) -> None:
    options = _preview_items(storage)
    choice = st.selectbox("Vista previa con", list(options), key="label_cfg_sample")
    try:
        layout = labels.layout_item_label(options[choice], spec)
    except ValueError as exc:
        st.error(f"No se puede imprimir este código en {spec.size_text}: {exc}")
        return
    image = labels.render_label(layout)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    width, height = spec.canvas_size
    st.image(
        buffer.getvalue(), width=min(width, 640),
        caption=f"Vista previa a resolución real: {spec.describe()} ({width} × {height} puntos)",
    )
    stats = st.columns(3)
    stats[0].metric("Texto más pequeño", f"{_decimal(layout.min_text_pt, 1)} pt")
    stats[1].metric("Alto de las barras", f"{_decimal(layout.bar_height_mm, 1)} mm")
    stats[2].metric("Ancho de barra mínima", f"{_decimal(layout.module_mm, 2)} mm")
    omitted = labels.describe_omitted(layout)
    if omitted:
        st.info(
            f"En {spec.size_text} no caben a un tamaño legible: **{', '.join(omitted)}**. "
            "Usa una etiqueta más grande o desactiva otro contenido para darles espacio."
        )
    if "name" in layout.shortened:
        st.warning("El nombre es largo para este tamaño y se recortó con «…».")
    for note in layout.warnings:
        st.warning(note)


def render(storage, user: dict) -> None:
    saved = labels.load_label_spec(storage)
    st.caption(
        f"Configuración guardada: **{saved.describe()}**. Todas las etiquetas de la app "
        "(Inventario, Escanear y registro de items) se generan con este tamaño."
    )
    spec, error = _spec_from_inputs(saved)
    if error:
        st.error(error)
        return

    _render_preview(storage, spec)

    save_col, test_col = st.columns(2)
    changed = spec != saved
    if save_col.button(
        "💾 Guardar configuración", type="primary", disabled=not changed,
        use_container_width=True, key="label_cfg_save",
    ):
        labels.save_label_spec(storage, spec, actor_email=user.get("institutional_email", ""))
        st.toast(f"Etiquetas configuradas a {spec.describe()}", icon="🏷️")
        st.rerun()
    test_col.download_button(
        "🖨️ Hoja de prueba de impresión (PDF)",
        data=labels.generate_calibration_pdf_bytes(spec),
        file_name=f"prueba_impresion_{spec.width_mm:g}x{spec.height_mm:g}mm.pdf",
        mime="application/pdf", use_container_width=True, key="label_cfg_test",
        help="Imprímela con la misma configuración que tus etiquetas y mide la regla.",
    )
    if changed:
        st.caption(
            "Hay cambios sin guardar: la vista previa y la hoja de prueba ya los usan, pero las "
            f"etiquetas del inventario siguen en {saved.describe()} hasta que guardes."
        )
    with st.expander("🖨️ Cómo imprimir a tamaño real (y qué hacer si la etiqueta sale pequeña)"):
        st.markdown(PRINT_GUIDE)
