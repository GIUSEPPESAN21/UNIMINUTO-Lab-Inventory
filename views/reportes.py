# -*- coding: utf-8 -*-
"""views/reportes.py - Analitica de uso del laboratorio, salud del inventario y
exportacion de datos. Visible para profesor/maestro."""

from datetime import datetime

import pandas as pd
import streamlit as st

from core import data_quality
from core import loans as loans_core
from core import permissions, reports
from core.ui import badge_html, empty_state, esc, guard_role, page_header, section_title, stat_cards

_PENDING_KEY = "dq_pending"      # correccion esperando confirmacion: {"key", "fixes"}
_FLASH_KEY = "dq_flash"          # resultado de la ultima correccion aplicada
_ALL_FIXES = "__todas__"

_GROUP_TITLES = {
    data_quality.SEVERITY_ERROR: "Errores",
    data_quality.SEVERITY_WARNING: "Avisos",
    data_quality.SEVERITY_INFO: "Sugerencias",
}
_SUBTLE = 'style="color: var(--subtle-text-color); font-size: 0.85rem;"'


def render():
    storage = st.session_state.storage

    if not guard_role(st.session_state.user, permissions.MANAGER_ROLES, "los Reportes"):
        return

    page_header("Reportes", icon="📊", subtitle="Analítica de uso y salud del inventario")

    try:
        items = storage.get_all_items(include_retired=True)
        users = storage.get_all_users()
        all_loans = storage.get_all_loans()
    except Exception as e:
        st.error(f"No se pudieron cargar los datos: {e}")
        items, users, all_loans = [], [], []

    tab1, tab_health, tab2, tab3 = st.tabs(
        ["📈 Uso del laboratorio", "🩺 Salud del inventario", "⚠️ Vencidos", "📥 Exportar base de datos"]
    )

    with tab1:
        c1, c2, c3 = st.columns(3)
        c1.metric("Prestamos historicos", len(all_loans))
        c2.metric("Duracion promedio de prestamo", f"{reports.average_loan_duration_hours(all_loans):.1f} h")
        c3.metric("Items activos", len([i for i in items if i.get("status") == "active"]))

        st.markdown("---")
        import plotly.express as px

        col_a, col_b = st.columns(2)
        with col_a:
            st.subheader("📅 Salidas por dia (ultimos 30 dias)")
            df_daily = reports.loans_per_day(all_loans)
            if df_daily.empty:
                st.info("Sin salidas registradas en los ultimos 30 dias.")
            else:
                fig = px.bar(df_daily, x="Fecha", y="Salidas")
                st.plotly_chart(fig, use_container_width=True)
        with col_b:
            st.subheader("🗂️ Items activos por categoria")
            df_cat = reports.items_by_category(items)
            if df_cat.empty:
                st.info("Sin items activos para graficar.")
            else:
                fig2 = px.pie(df_cat, names="Categoria", values="Items", hole=0.45)
                st.plotly_chart(fig2, use_container_width=True)

        st.markdown("---")
        st.subheader("🏆 Items mas prestados")
        st.dataframe(reports.most_borrowed_items(all_loans), use_container_width=True, hide_index=True)

        st.subheader("👤 Usuarios con mas prestamos")
        st.dataframe(reports.top_users_by_loans(all_loans), use_container_width=True, hide_index=True)

    with tab_health:
        _render_health(storage, st.session_state.user, items)

    with tab2:
        overdue = loans_core.get_overdue_loans(storage)
        if not overdue:
            st.success("No hay prestamos vencidos.")
        for loan in overdue:
            st.error(
                f"**{loan['item_name']}** x{loan['quantity']} · prestado a {loan['user_name']} "
                f"· debia devolverse antes del {loan['expected_return_at'].strftime('%d/%m/%Y')}"
            )

    with tab3:
        st.subheader("Descarga masiva de la base de datos")
        st.caption("Genera un Excel con items, usuarios (sin contrasenas) y prestamos.")
        if st.button("📥 Generar Excel", type="primary"):
            buf = reports.export_full_database(items, users, all_loans)
            st.session_state["export_buffer"] = buf

        if "export_buffer" in st.session_state:
            st.download_button(
                "⬇️ Descargar Excel",
                data=st.session_state["export_buffer"],
                file_name=f"Inventario_UNIMINUTO_Export_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )


# ---------------------------------------------------------------------------
# 🩺 Salud del inventario
# ---------------------------------------------------------------------------

def _render_health(storage, user: dict, items: list) -> None:
    section_title(
        "Salud del inventario", icon="🩺",
        caption="Revisión automática de códigos, jerarquía, ubicaciones, nombres, cantidades y contenido. "
                "Nada se modifica sin tu confirmación.",
    )
    _show_flash()

    findings = data_quality.audit_items(items)
    summary = data_quality.summarize(findings, items)
    stat_cards([
        {"label": "Ítems revisados", "value": summary["items"], "icon": "📦"},
        {"label": "Sin problemas", "value": summary["healthy_items"], "icon": "✅",
         "tone": "success" if summary["items"] and summary["healthy_items"] == summary["items"] else "neutral"},
        {"label": "Errores", "value": summary["errors"], "icon": "⛔",
         "tone": "danger" if summary["errors"] else "neutral"},
        {"label": "Avisos", "value": summary["warnings"], "icon": "⚠️",
         "tone": "warning" if summary["warnings"] else "neutral"},
        {"label": "Sugerencias", "value": summary["infos"], "icon": "💡",
         "tone": "info" if summary["infos"] else "neutral"},
    ])

    if not summary["items"]:
        empty_state("El inventario está vacío",
                    "Cuando registres productos, aquí verás su revisión de calidad.", icon="📦")
        return
    if not findings:
        empty_state("Todo en orden", "No se encontraron problemas en el inventario.", icon="✅")
        return

    merged = data_quality.merge_fixes(findings)
    _drop_orphan_pending({f.key for f in findings}, bool(merged))
    if merged:
        _render_bulk_fixes(storage, user, merged)

    grouped = data_quality.group_by_severity(findings)
    for severity in data_quality.SEVERITIES:
        group = grouped.get(severity) or []
        if not group:
            continue
        title = f"{data_quality.SEVERITY_ICONS[severity]} {_GROUP_TITLES[severity]} ({len(group)})"
        with st.expander(title, expanded=severity != data_quality.SEVERITY_INFO):
            for finding in group:
                _render_finding(storage, user, finding)

    st.download_button(
        "⬇️ Descargar hallazgos (CSV)", data=_findings_csv(findings),
        file_name=f"salud_inventario_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv", key="dq_download", width="stretch",
    )


def _render_finding(storage, user: dict, finding) -> None:
    severity = finding.severity
    with st.container(border=True):
        badge = badge_html(data_quality.SEVERITY_LABELS[severity], data_quality.SEVERITY_TONES[severity],
                           data_quality.SEVERITY_ICONS[severity])
        who = esc(finding.item_name or ("Inventario completo" if not finding.item_id else "Sin nombre"))
        code = f" <code>{esc(finding.item_id)}</code>" if finding.item_id else ""
        st.markdown(f"{badge} <strong>{who}</strong>{code} · {esc(finding.field_label)}",
                    unsafe_allow_html=True)
        st.markdown(f"<div>{esc(finding.message)}</div>", unsafe_allow_html=True)
        if finding.details:
            rows = "".join(f"<li>{esc(detail)}</li>" for detail in finding.details)
            st.markdown(f'<ul style="margin: 0.25rem 0 0.25rem 1rem;">{rows}</ul>', unsafe_allow_html=True)
        if finding.suggestion:
            st.markdown(f"<div {_SUBTLE}>💡 {esc(finding.suggestion)}</div>", unsafe_allow_html=True)
        if finding.fix:
            fixes = {finding.item_id: {**finding.fix, "item_name": finding.item_name}}
            _render_fix_controls(storage, user, finding.key, fixes, button_label=f"🛠️ {finding.fix['label']}")


def _render_bulk_fixes(storage, user: dict, merged: dict) -> None:
    changes = sum(len(fix["changes"]) for fix in merged.values())
    with st.container(border=True):
        st.markdown(
            f"**🛠️ Correcciones seguras disponibles:** {changes} cambio(s) en {len(merged)} ítem(s)."
        )
        st.markdown(
            f"<div {_SUBTLE}>Solo cambian textos que se deducen sin ambigüedad: la ubicación desde el código, "
            "errores de digitación de palabras conocidas y categorías escritas de varias formas. "
            "Cada cambio queda en el historial del ítem.</div>",
            unsafe_allow_html=True,
        )
        _render_fix_controls(storage, user, _ALL_FIXES, merged,
                             button_label=f"Revisar y aplicar todas ({changes})")


def _render_fix_controls(storage, user: dict, key: str, fixes: dict, button_label: str) -> None:
    """Boton que propone la correccion; al pulsarlo muestra el antes/despues y
    solo escribe tras una confirmacion explicita."""
    pending = st.session_state.get(_PENDING_KEY)
    if not pending or pending.get("key") != key:
        if st.button(button_label, key=f"dq_fix_{key}"):
            # Se guarda la propuesta EXACTA que se muestra: al confirmar se valida
            # contra la base actual y se rechaza si el ítem cambió entretanto.
            st.session_state[_PENDING_KEY] = {"key": key, "fixes": fixes}
            st.rerun()
        return

    st.markdown("**Revisa el cambio antes de guardarlo:**")
    st.markdown(_diff_html(pending["fixes"]), unsafe_allow_html=True)
    confirm_col, cancel_col = st.columns(2)
    if confirm_col.button("✅ Confirmar y guardar", key=f"dq_confirm_{key}", type="primary",
                          width="stretch"):
        with st.spinner("Guardando correcciones..."):
            applied, errors = _apply_fixes(storage, user, pending["fixes"])
        st.session_state[_PENDING_KEY] = None
        st.session_state[_FLASH_KEY] = {"applied": applied, "errors": errors}
        st.rerun()
    if cancel_col.button("Cancelar", key=f"dq_cancel_{key}", width="stretch"):
        st.session_state[_PENDING_KEY] = None
        st.rerun()


def _apply_fixes(storage, user: dict, fixes: dict):
    """Aplica cada correccion con storage.save_item. Devuelve (aplicados, errores)."""
    applied, errors = [], []
    actor = user.get("institutional_email", "")
    for item_id, fix in fixes.items():
        try:
            data = data_quality.apply_fix(storage.get_item(item_id), fix)
            storage.save_item(data, item_id, is_new=False, actor_email=actor,
                              details=f"Corrección de calidad de datos: {fix['label']}.")
            applied.append(item_id)
        except ValueError as exc:  # incluye StaleFixError y validaciones de storage
            errors.append(f"{item_id}: {exc}")
        except Exception as exc:  # fallo de E/S o de red: se informa, no se oculta
            errors.append(f"{item_id}: no se pudo guardar ({exc})")
    return applied, errors


def _diff_html(fixes: dict) -> str:
    rows = []
    for item_id, fix in fixes.items():
        name = fix.get("item_name") or item_id
        for field_name, after in fix["changes"].items():
            before = fix["before"].get(field_name, "")
            rows.append(
                f"<tr><td><strong>{esc(name)}</strong><br><code>{esc(item_id)}</code></td>"
                f"<td>{esc(data_quality.FIELD_LABELS.get(field_name, field_name))}</td>"
                f"<td>{esc(before) or '<em>(vacío)</em>'}</td><td>{esc(after)}</td></tr>"
            )
    return (
        '<table style="width:100%; font-size:0.9rem;"><thead><tr><th>Ítem</th><th>Campo</th>'
        f"<th>Actual</th><th>Propuesto</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _show_flash() -> None:
    flash = st.session_state.pop(_FLASH_KEY, None)
    if not flash:
        return
    if flash["applied"]:
        st.success(f"Corrección aplicada en {len(flash['applied'])} ítem(s): {', '.join(flash['applied'])}.")
    for error in flash["errors"]:
        st.warning(f"No se aplicó: {error}")


def _drop_orphan_pending(current_keys: set, has_bulk: bool) -> None:
    """Olvida (y lo avisa) una propuesta pendiente cuyo hallazgo ya no existe:
    alguien corrigió o cambió el ítem mientras se revisaba."""
    pending = st.session_state.get(_PENDING_KEY)
    if not pending:
        return
    key = pending.get("key")
    if (key == _ALL_FIXES and not has_bulk) or (key != _ALL_FIXES and key not in current_keys):
        st.session_state[_PENDING_KEY] = None
        st.warning("La corrección que estabas revisando ya no aplica: el ítem cambió o ya fue corregido. "
                   "No se guardó nada; revisa los hallazgos actualizados.")


def _csv_cell(value) -> str:
    """Evita que Excel interprete como formula un texto escrito por usuarios."""
    text = "" if value is None else str(value)
    return f"'{text}" if text[:1] in ("=", "+", "-", "@") else text


def _findings_csv(findings: list) -> bytes:
    rows = [{
        "severidad": data_quality.SEVERITY_LABELS[f.severity], "codigo": f.item_id, "item": f.item_name,
        "campo": f.field_label, "hallazgo": f.message, "sugerencia": f.suggestion,
        "detalle": " | ".join(f.details), "correccion_segura": (f.fix or {}).get("label", ""),
    } for f in findings]
    rows = [{column: _csv_cell(value) for column, value in row.items()} for row in rows]
    # utf-8-sig: Excel en Windows abre bien las tildes.
    return pd.DataFrame(rows).to_csv(index=False).encode("utf-8-sig")
