# -*- coding: utf-8 -*-
"""
core/ui.py - Componentes visuales compartidos para que TODA la app tenga el
mismo look (encabezados, tarjetas, indicadores, estados) sobre el sistema de
diseño de style.css.

Contrato estable (lo usan varias vistas; se puede mejorar su HTML interno,
pero NO cambiar firmas ni nombres de clases CSS):
    esc, page_header, badge_html, status_badge, section_title, stat_cards,
    empty_state, timeline_html, timeline, quick_actions.
    Tonos: neutral, info, success, warning, danger.

Componentes adicionales (también estables, documentados en cada función):
    hero(...)              banda de bienvenida destacada         (.lab-hero)
    card(key, tone)        st.container con estilo de tarjeta    ([st-key-lab_card...])
    card_header(...)       título + subtítulo + pastillas        (.lab-card-head)
    kv_list(rows)          lista clave/valor                     (.lab-kv)
    feature_cards(items)   rejilla de tarjetas informativas      (.lab-feature-grid)
    alert_list(items)      lista de avisos con tono              (.lab-alert-list)
    user_chip_html(...)    tarjeta de usuario del sidebar        (.user-chip)
    greeting(), long_date_es(), initials(), logo_data_uri(), footer()

Todo texto se escapa con esc(): puede venir de usuarios.
"""

import base64
import html
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import streamlit as st

from core import permissions

LOGO_PATH = Path(__file__).resolve().parent.parent / "assets" / "uniminuto-logo.png"
ROLE_LABELS = {"estudiante": "Estudiante", "profesor": "Profesor", "maestro": "Perfil maestro"}

_WEEKDAYS_ES = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_MONTHS_ES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
              "septiembre", "octubre", "noviembre", "diciembre")


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
    cap_html = (
        f'<div style="color: var(--subtle-text-color); font-size: 0.8rem; margin-top: 4px;">{esc(caption)}</div>'
        if caption else ""
    )
    st.markdown(
        f'<div style="text-align:center;">'
        f'<img src="{esc(url)}" width="{int(width)}" alt="" style="display:inline-block;">'
        f"{cap_html}"
        f"</div>",
        unsafe_allow_html=True,
    )


@lru_cache(maxsize=1)
def logo_data_uri() -> str:
    """Logo institucional (assets/uniminuto-logo.png) como data URI: funciona sin
    Internet y no depende de un servidor externo. Cadena vacia si falta."""
    try:
        return "data:image/png;base64," + base64.b64encode(LOGO_PATH.read_bytes()).decode("ascii")
    except OSError:
        return ""


def initials(name) -> str:
    """Inicial para avatares ("?" si no hay nombre)."""
    text = str(name or "").strip()
    return text[:1].upper() if text else "?"


def greeting(now: datetime = None) -> str:
    """Saludo segun la hora local (Bogota) del momento."""
    if now is None:
        from core.reservations import BOGOTA_TZ
        now = datetime.now(BOGOTA_TZ)
    if 5 <= now.hour < 12:
        return "Buenos días"
    if 12 <= now.hour < 19:
        return "Buenas tardes"
    return "Buenas noches"


def long_date_es(now: datetime = None) -> str:
    """Fecha larga en español, independiente del locale del servidor."""
    if now is None:
        from core.reservations import BOGOTA_TZ
        now = datetime.now(BOGOTA_TZ)
    return f"{_WEEKDAYS_ES[now.weekday()]}, {now.day} de {_MONTHS_ES[now.month - 1]} de {now.year}"


def page_header(title: str, subtitle: str = None, icon: str = None) -> None:
    """Encabezado de pagina consistente para todas las vistas (tarjeta con
    icono, titulo y subtitulo). Todo se escapa: puede incluir nombres
    escritos por usuarios."""
    icon_html = f'<div class="lab-page-header__icon" aria-hidden="true">{esc(icon)}</div>' if icon else ""
    sub_html = f'<p class="page-subtitle">{esc(subtitle)}</p>' if subtitle else ""
    st.markdown(
        f'<div class="lab-page-header">{icon_html}<div class="lab-page-header__text">'
        f'<div class="main-header" role="heading" aria-level="1">{esc(title)}</div>{sub_html}</div></div>',
        unsafe_allow_html=True,
    )


def hero(title: str, subtitle: str = None, eyebrow: str = None, icon: str = None, badges: list = None) -> None:
    """Banda destacada (bienvenida del inicio). badges: lista de dicts
    {"text", "tone"?, "icon"?} que se pintan con badge_html."""
    eyebrow_html = f'<div class="lab-hero__eyebrow">{esc(eyebrow)}</div>' if eyebrow else ""
    sub_html = f'<div class="lab-hero__text">{esc(subtitle)}</div>' if subtitle else ""
    meta = "".join(badge_html(b.get("text"), b.get("tone", "neutral"), b.get("icon")) for b in (badges or []))
    meta_html = f'<div class="lab-hero__meta">{meta}</div>' if meta else ""
    icon_html = f'<div class="lab-hero__icon" aria-hidden="true">{esc(icon)}</div>' if icon else ""
    st.markdown(
        f'<section class="lab-hero"><div class="lab-hero__body">{eyebrow_html}'
        f'<div class="lab-hero__title" role="heading" aria-level="1">{esc(title)}</div>'
        f'{sub_html}{meta_html}</div>{icon_html}</section>',
        unsafe_allow_html=True,
    )


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
    prefix = f'<span aria-hidden="true">{esc(icon)}</span> ' if icon else ""
    return f'<span class="lab-badge lab-badge--{_tone(tone)}">{prefix}{esc(text)}</span>'


def status_badge(text, tone: str = "neutral", icon: str = None) -> None:
    st.markdown(badge_html(text, tone, icon), unsafe_allow_html=True)


def section_title(title: str, icon: str = None, caption: str = None) -> None:
    """Titulo de seccion dentro de una pagina (mas liviano que page_header)."""
    heading = f"{esc(icon)} {esc(title)}" if icon else esc(title)
    cap = f'<div class="lab-section__caption">{esc(caption)}</div>' if caption else ""
    st.markdown(
        f'<div class="lab-section"><div class="lab-section__title" role="heading" aria-level="2">{heading}</div>'
        f'{cap}</div>',
        unsafe_allow_html=True,
    )


def stat_cards(stats: list) -> None:
    """Fila de indicadores. Cada stat: {"label", "value", "icon"?, "tone"?, "help"?}."""
    cards = []
    for stat in stats:
        icon = (
            f'<div class="lab-stat__icon" aria-hidden="true">{esc(stat.get("icon"))}</div>'
            if stat.get("icon") else ""
        )
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
        f'<div class="lab-empty"><div class="lab-empty__icon" aria-hidden="true">{esc(icon)}</div>'
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
        current = ' aria-current="step"' if state == "current" else ""
        rows.append(
            f'<li class="lab-step lab-step--{state}"{current}>'
            f'<div class="lab-step__marker" aria-hidden="true">{icon}</div>'
            f'<div class="lab-step__body"><div class="lab-step__title">{esc(step.get("title"))}</div>'
            f'{detail}{when}</div></li>'
        )
    return f'<ol class="lab-timeline" style="list-style:none;padding:0;">{"".join(rows)}</ol>'


def timeline(steps: list) -> None:
    st.markdown(timeline_html(steps), unsafe_allow_html=True)


def quick_actions(actions: list, columns: int = 3) -> None:
    """Tarjetas de acceso rapido (toda la tarjeta es clicable). Cada accion:
    {"page": st.Page, "label", "icon"?, "description"?}. Las acciones sin
    pagina se omiten. Se pintan por filas para conservar el orden en celular."""
    actions = [action for action in actions if action.get("page") is not None]
    if not actions:
        return
    per_row = max(1, min(columns, len(actions)))
    for start in range(0, len(actions), per_row):
        cols = st.columns(per_row)
        for col, action in zip(cols, actions[start:start + per_row]):
            with col:
                with st.container(border=True):
                    st.page_link(action["page"], label=action["label"], icon=action.get("icon"),
                                 use_container_width=True)
                    description = action.get("description")
                    if description:
                        st.markdown(f'<div class="lab-qa">{esc(description)}</div>', unsafe_allow_html=True)
                    else:
                        st.markdown('<div class="lab-qa lab-qa--empty"></div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Componentes adicionales
# ---------------------------------------------------------------------------

def card(key: str, tone: str = None):
    """Contenedor con estilo de tarjeta (fondo, sombra, hover). Usar como
    `with card("prestamo_123"):`. La key debe ser unica en la pagina; tone
    (info/success/warning/danger) agrega un filete lateral de color."""
    prefix = f"lab_card_{tone}_" if tone in TONES and tone != "neutral" else "lab_card_"
    return st.container(border=True, key=f"{prefix}{key}")


def card_header_html(title, subtitle: str = None, icon: str = None, badges: list = None) -> str:
    """Encabezado de tarjeta: icono, titulo, subtitulo y pastillas
    (badges: lista de dicts {"text", "tone"?, "icon"?})."""
    icon_html = f'<div class="lab-card-head__icon" aria-hidden="true">{esc(icon)}</div>' if icon else ""
    sub_html = f'<div class="lab-card-head__subtitle">{esc(subtitle)}</div>' if subtitle else ""
    pills = "".join(badge_html(b.get("text"), b.get("tone", "neutral"), b.get("icon")) for b in (badges or []))
    pills_html = f'<div class="lab-card-head__badges">{pills}</div>' if pills else ""
    return (
        f'<div class="lab-card-head">{icon_html}<div class="lab-card-head__text">'
        f'<div class="lab-card-head__title">{esc(title)}</div>{sub_html}</div>{pills_html}</div>'
    )


def card_header(title, subtitle: str = None, icon: str = None, badges: list = None) -> None:
    st.markdown(card_header_html(title, subtitle, icon, badges), unsafe_allow_html=True)


def kv_list(rows: list) -> None:
    """Lista clave/valor. rows: [(etiqueta, valor), ...]; valores vacios -> 'N/A'."""
    items = "".join(
        f'<div class="lab-kv__item"><dt class="lab-kv__label">{esc(label)}</dt>'
        f'<dd class="lab-kv__value">{esc(value if value not in (None, "") else "N/A")}</dd></div>'
        for label, value in rows
    )
    st.markdown(f'<dl class="lab-kv">{items}</dl>', unsafe_allow_html=True)


def feature_cards(items: list) -> None:
    """Rejilla de tarjetas informativas. Cada item: {"title", "text", "icon"?}."""
    cards = []
    for item in items:
        icon = (
            f'<div class="lab-feature__icon" aria-hidden="true">{esc(item.get("icon"))}</div>'
            if item.get("icon") else ""
        )
        cards.append(
            f'<div class="lab-feature">{icon}<div class="lab-feature__title">{esc(item.get("title"))}</div>'
            f'<div class="lab-feature__text">{esc(item.get("text"))}</div></div>'
        )
    cards = "".join(cards)
    st.markdown(f'<div class="lab-feature-grid">{cards}</div>', unsafe_allow_html=True)


def alert_list(items: list, label: str = "Alertas") -> None:
    """Lista de avisos con tono. Cada item: {"title", "text"?, "tone"?, "icon"?}.
    La lista es desplazable y enfocable con teclado cuando es larga."""
    rows = []
    for item in items:
        icon = f'<div class="lab-alert__icon" aria-hidden="true">{esc(item.get("icon"))}</div>' if item.get("icon") else ""
        text = f'<div class="lab-alert__text">{esc(item.get("text"))}</div>' if item.get("text") else ""
        rows.append(
            f'<div class="lab-alert lab-alert--{_tone(item.get("tone", "neutral"))}" role="listitem">{icon}'
            f'<div class="lab-alert__body"><div class="lab-alert__title">{esc(item.get("title"))}</div>{text}</div></div>'
        )
    st.markdown(
        f'<div class="lab-alert-list" role="list" tabindex="0" aria-label="{esc(label)}">{"".join(rows)}</div>',
        unsafe_allow_html=True,
    )


def user_chip_html(user: dict) -> str:
    """Tarjeta de usuario (avatar, nombre y rol) para la barra lateral."""
    role = user.get("role", "")
    return (
        f'<div class="user-chip"><div class="user-avatar" aria-hidden="true">{esc(initials(user.get("full_name")))}</div>'
        f'<div><div class="user-name">{esc(user.get("full_name"))}</div>'
        f'<div class="user-role role-{esc(role)}">{esc(ROLE_LABELS.get(role, role))}</div></div></div>'
    )


def footer(text: str = "© 2026 UNIMINUTO · Laboratorio de Ingeniería") -> None:
    st.markdown(f'<div class="lab-footer">{esc(text)}</div>', unsafe_allow_html=True)


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
            "**La sincronizacion con GitHub no esta configurada**: los datos se estan "
            "guardando solo en este contenedor y se **perderan** al reiniciarse la app. "
            "Agrega `GITHUB_TOKEN` y `GITHUB_REPO` en Settings → Secrets de Streamlit Cloud.",
            icon="🔴",
        )
        return

    if status.get("conflict"):
        st.error(f"**Conflicto de sincronizacion con GitHub.** {status['message']}", icon="🔴")
        _render_conflict_controls(storage, user)
        return

    if status["ok"] is False:
        st.error(
            f"**Fallo la sincronizacion con GitHub** ({status['repo'] or 'repo no configurado'} · "
            f"`{status['db_path']}`): {status['message']} Los cambios se estan guardando solo "
            "localmente y se perderan al reiniciarse la app.",
            icon="🔴",
        )
        if st.button("🔄 Reintentar sincronización", key="sync_retry"):
            storage.retry_sync()
            st.rerun()
