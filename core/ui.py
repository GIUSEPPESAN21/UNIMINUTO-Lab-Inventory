# -*- coding: utf-8 -*-
"""
core/ui.py - Componentes visuales compartidos para que TODA la app tenga el
mismo look centrado y organizado (logo, encabezados de pagina).
"""

import html

import streamlit as st

from core import permissions


def esc(value) -> str:
    """Escapa texto de usuario antes de incrustarlo en HTML (unsafe_allow_html)."""
    return html.escape("" if value is None else str(value), quote=True)


def guard_role(user: dict, roles, section: str = "esta seccion") -> bool:
    """Comprueba el rol dentro de la propia vista (defensa en profundidad: la
    navegacion ya oculta la pagina, pero ocultar no es autorizar). Si no hay
    permiso muestra el aviso y devuelve False; la vista debe terminar."""
    if permissions.has_role(user, roles):
        return True
    st.error(f"No tienes permiso para acceder a {section}.")
    return False


def centered_logo(url: str, width: int = 110, caption: str = None) -> None:
    """Renderiza una imagen perfectamente centrada via HTML plano, sin
    depender de trucos de columnas (que no centran el contenido real)."""
    cap_html = f'<div style="color: var(--subtle-text-color); font-size: 0.8rem; margin-top: 4px;">{caption}</div>' if caption else ""
    st.markdown(
        f'<div style="text-align:center;">'
        f'<img src="{url}" width="{width}" style="display:inline-block;">'
        f"{cap_html}"
        f"</div>",
        unsafe_allow_html=True,
    )


def page_header(title: str, subtitle: str = None, icon: str = None) -> None:
    """Encabezado de pagina centrado y consistente para todas las vistas.
    Titulo y subtitulo se escapan: pueden incluir nombres escritos por usuarios."""
    heading = f"{icon} {esc(title)}" if icon else esc(title)
    st.markdown(f'<h1 class="main-header">{heading}</h1>', unsafe_allow_html=True)
    if subtitle:
        st.markdown(f'<p class="page-subtitle">{esc(subtitle)}</p>', unsafe_allow_html=True)
    st.markdown("<hr>", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Componentes compartidos (contrato estable: las vistas los usan y style.css
# los estiliza; se puede mejorar su HTML interno, pero NO cambiar firmas ni
# nombres de clases CSS). Todo texto se escapa: puede venir de usuarios.
# ---------------------------------------------------------------------------

TONES = ("neutral", "info", "success", "warning", "danger")
STEP_STATES = ("done", "current", "pending", "blocked")


def _tone(tone: str) -> str:
    return tone if tone in TONES else "neutral"


def badge_html(text, tone: str = "neutral", icon: str = None) -> str:
    """Pastilla de estado como HTML (para incrustar en otras piezas)."""
    prefix = f"{esc(icon)} " if icon else ""
    return f'<span class="lab-badge lab-badge--{_tone(tone)}">{prefix}{esc(text)}</span>'


def status_badge(text, tone: str = "neutral", icon: str = None) -> None:
    st.markdown(badge_html(text, tone, icon), unsafe_allow_html=True)


def section_title(title: str, icon: str = None, caption: str = None) -> None:
    """Titulo de seccion dentro de una pagina (mas liviano que page_header)."""
    heading = f"{esc(icon)} {esc(title)}" if icon else esc(title)
    cap = f'<div class="lab-section__caption">{esc(caption)}</div>' if caption else ""
    st.markdown(f'<div class="lab-section"><div class="lab-section__title">{heading}</div>{cap}</div>',
                unsafe_allow_html=True)


def stat_cards(stats: list) -> None:
    """Fila de indicadores. Cada stat: {"label", "value", "icon"?, "tone"?, "help"?}."""
    cards = []
    for stat in stats:
        icon = f'<div class="lab-stat__icon">{esc(stat.get("icon"))}</div>' if stat.get("icon") else ""
        help_text = f'<div class="lab-stat__help">{esc(stat.get("help"))}</div>' if stat.get("help") else ""
        cards.append(
            f'<div class="lab-stat lab-stat--{_tone(stat.get("tone", "neutral"))}">{icon}'
            f'<div class="lab-stat__value">{esc(stat.get("value"))}</div>'
            f'<div class="lab-stat__label">{esc(stat.get("label"))}</div>{help_text}</div>'
        )
    st.markdown(f'<div class="lab-stat-grid">{"".join(cards)}</div>', unsafe_allow_html=True)


def empty_state(title: str, text: str = "", icon: str = "📭") -> None:
    body = f'<div class="lab-empty__text">{esc(text)}</div>' if text else ""
    st.markdown(
        f'<div class="lab-empty"><div class="lab-empty__icon">{esc(icon)}</div>'
        f'<div class="lab-empty__title">{esc(title)}</div>{body}</div>',
        unsafe_allow_html=True,
    )


def timeline_html(steps: list) -> str:
    """Linea de tiempo vertical. Cada paso: {"title", "detail"?, "time"?, "icon"?,
    "state": done|current|pending|blocked}."""
    rows = []
    for index, step in enumerate(steps, start=1):
        state = step.get("state") if step.get("state") in STEP_STATES else "pending"
        icon = esc(step.get("icon") or ("✓" if state == "done" else index))
        detail = f'<div class="lab-step__detail">{esc(step.get("detail"))}</div>' if step.get("detail") else ""
        when = f'<div class="lab-step__time">{esc(step.get("time"))}</div>' if step.get("time") else ""
        rows.append(
            f'<div class="lab-step lab-step--{state}"><div class="lab-step__marker">{icon}</div>'
            f'<div class="lab-step__body"><div class="lab-step__title">{esc(step.get("title"))}</div>'
            f'{detail}{when}</div></div>'
        )
    return f'<div class="lab-timeline">{"".join(rows)}</div>'


def timeline(steps: list) -> None:
    st.markdown(timeline_html(steps), unsafe_allow_html=True)


def quick_actions(actions: list, columns: int = 3) -> None:
    """Tarjetas de acceso rapido. Cada accion: {"page": st.Page, "label", "icon"?,
    "description"?}. Las acciones sin pagina se omiten."""
    actions = [action for action in actions if action.get("page") is not None]
    if not actions:
        return
    cols = st.columns(min(columns, len(actions)))
    for index, action in enumerate(actions):
        with cols[index % len(cols)]:
            with st.container(border=True):
                st.page_link(action["page"], label=action["label"], icon=action.get("icon"),
                             use_container_width=True)
                if action.get("description"):
                    st.caption(action["description"])


def _render_conflict_controls(storage, user: dict) -> None:
    """Acciones para resolver un conflicto de sincronizacion (solo maestro)."""
    if user.get("role") != permissions.ROLE_MASTER:
        st.info("Pide al perfil maestro que resuelva el conflicto desde cualquier pagina de la app.")
        return
    keep_remote, keep_local = st.columns(2)
    if keep_remote.button(
        "⬇️ Conservar la versión de GitHub", key="sync_keep_remote", use_container_width=True,
        help="Descarta los cambios hechos desde el conflicto y carga la base que esta en GitHub.",
    ):
        ok, message = storage.resolve_sync_conflict("remote")
        st.toast(message, icon="✅" if ok else "⚠️")
        if ok:
            st.rerun()
    if keep_local.button(
        "⬆️ Conservar la versión de esta app", key="sync_keep_local", use_container_width=True,
        help="Publica esta version sobre la de GitHub. La anterior sigue en el historial de commits.",
    ):
        ok, message = storage.resolve_sync_conflict("local")
        st.toast(message, icon="✅" if ok else "⚠️")
        if ok:
            st.rerun()


def sync_status_banner(storage, user: dict) -> None:
    """Aviso visible (solo para profesor/maestro) cuando la sincronizacion
    con GitHub no esta configurada o el ultimo intento fallo: sin esto, los
    datos se guardan solo en el disco temporal de la app y se PIERDEN al
    reiniciarse, sin que nadie lo note (el error solo queda en los logs del
    servidor, no en la interfaz)."""
    if user.get("role") not in permissions.MANAGER_ROLES:
        return

    status = storage.get_sync_status()

    if not status["configured"]:
        st.error(
            "🔴 **La sincronizacion con GitHub no esta configurada**: los datos se estan "
            "guardando solo en este contenedor y se **perderan** al reiniciarse la app. "
            "Agrega `GITHUB_TOKEN` y `GITHUB_REPO` en Settings → Secrets de Streamlit Cloud.",
            icon="🔴",
        )
        return

    if status.get("conflict"):
        st.error(f"🔴 **Conflicto de sincronizacion con GitHub.** {status['message']}", icon="🔴")
        _render_conflict_controls(storage, user)
        return

    if status["ok"] is False:
        st.error(
            f"🔴 **Fallo la sincronizacion con GitHub** ({status['repo'] or 'repo no configurado'} · "
            f"`{status['db_path']}`): {status['message']} Los cambios se estan guardando solo "
            "localmente y se perderan al reiniciarse la app.",
            icon="🔴",
        )
        if st.button("🔄 Reintentar sincronización", key="sync_retry"):
            storage.retry_sync()
            st.rerun()
