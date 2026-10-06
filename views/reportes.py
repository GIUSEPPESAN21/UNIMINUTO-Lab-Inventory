# -*- coding: utf-8 -*-
"""views/reportes.py - Analitica de uso del laboratorio y exportacion de datos.
Visible para profesor/maestro."""

from datetime import datetime

import streamlit as st

from core import loans as loans_core
from core import permissions, reports
from core.ui import guard_role, page_header


def render():
    storage = st.session_state.storage

    if not guard_role(st.session_state.user, permissions.MANAGER_ROLES, "los Reportes"):
        return

    page_header("Reportes", icon="📊", subtitle="Analítica de uso del laboratorio")

    try:
        items = storage.get_all_items(include_retired=True)
        users = storage.get_all_users()
        all_loans = storage.get_all_loans()
    except Exception as e:
        st.error(f"No se pudieron cargar los datos: {e}")
        items, users, all_loans = [], [], []

    tab1, tab2, tab3 = st.tabs(["📈 Uso del laboratorio", "⚠️ Vencidos", "📥 Exportar base de datos"])

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
