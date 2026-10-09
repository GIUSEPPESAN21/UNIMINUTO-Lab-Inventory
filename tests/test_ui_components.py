# -*- coding: utf-8 -*-
"""Componentes visuales compartidos (core/ui.py) y contrato de style.css:
todo texto se escapa, los tonos/estados desconocidos caen en un valor seguro y
la hoja de estilos conserva las clases del contrato, el modo oscuro y el
respeto por prefers-reduced-motion."""

import re
from datetime import datetime
from pathlib import Path

import pytest

from core import ui

XSS = '<img src=x onerror="alert(1)">'
CSS = (Path(__file__).resolve().parent.parent / "style.css").read_text(encoding="utf-8")


def _no_raw_xss(html: str) -> None:
    assert "<img src=x" not in html
    assert "&lt;img src=x" in html


# --- HTML de los componentes --------------------------------------------------------

def test_badge_escapes_text_and_icon_and_falls_back_to_neutral():
    html = ui.badge_html(XSS, tone="rainbow", icon="<b>")
    _no_raw_xss(html)
    assert "lab-badge lab-badge--neutral" in html
    assert "<b>" not in html


@pytest.mark.parametrize("tone", ui.TONES)
def test_badge_keeps_every_contract_tone(tone):
    assert f"lab-badge--{tone}" in ui.badge_html("x", tone)


def test_timeline_escapes_and_marks_states():
    html = ui.timeline_html([
        {"title": XSS, "detail": XSS, "time": "08:00", "state": "done"},
        {"title": "Revisión", "state": "current"},
        {"title": "Entrega", "state": "inventado"},
        {"title": "Rechazo", "state": "blocked"},
    ])
    _no_raw_xss(html)
    assert "lab-timeline" in html
    for state in ("done", "current", "pending", "blocked"):
        assert f"lab-step lab-step--{state}" in html
    assert 'aria-current="step"' in html
    assert html.count("lab-step__marker") == 4


def test_card_header_escapes_everything():
    html = ui.card_header_html(XSS, subtitle=XSS, icon="<i>", badges=[{"text": XSS, "tone": "danger"}])
    _no_raw_xss(html)
    assert "<i>" not in html
    assert "lab-badge--danger" in html


def test_user_chip_escapes_name_and_role():
    html = ui.user_chip_html({"full_name": "<script>alert(1)</script> Pérez", "role": '"><b>'})
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert '"><b>' not in html
    assert 'class="user-avatar"' in html and "user-name" in html


def test_user_chip_uses_role_label_and_class():
    html = ui.user_chip_html({"full_name": "Ana", "role": "maestro"})
    assert "role-maestro" in html and "Perfil maestro" in html


def test_initials_handles_empty_names():
    assert ui.initials("laura gómez") == "L"
    assert ui.initials("") == "?"
    assert ui.initials(None) == "?"


@pytest.mark.parametrize("hour, expected", [(5, "Buenos días"), (11, "Buenos días"), (12, "Buenas tardes"),
                                            (18, "Buenas tardes"), (19, "Buenas noches"), (2, "Buenas noches")])
def test_greeting_depends_on_the_hour(hour, expected):
    assert ui.greeting(datetime(2026, 10, 9, hour, 30)) == expected


def test_long_date_is_spanish_and_locale_independent():
    assert ui.long_date_es(datetime(2026, 10, 9)) == "viernes, 9 de octubre de 2026"
    assert ui.long_date_es(datetime(2026, 1, 4)) == "domingo, 4 de enero de 2026"


def test_logo_is_served_from_the_repository_assets():
    uri = ui.logo_data_uri()
    assert uri.startswith("data:image/png;base64,") and len(uri) > 1000


def test_card_uses_a_styled_key_with_optional_tone(monkeypatch):
    calls = []
    monkeypatch.setattr(ui.st, "container", lambda **kwargs: calls.append(kwargs))
    ui.card("prestamo_1")
    ui.card("prestamo_2", tone="danger")
    ui.card("prestamo_3", tone="desconocido")
    assert [c["key"] for c in calls] == ["lab_card_prestamo_1", "lab_card_danger_prestamo_2", "lab_card_prestamo_3"]
    assert all(c["border"] is True for c in calls)


# --- Componentes renderizados (AppTest) ----------------------------------------------

pytest.importorskip("streamlit.testing.v1")
from streamlit.testing.v1 import AppTest  # noqa: E402


def _render_all_script():
    from core import ui

    bad = '<img src=x onerror="alert(1)">'
    ui.page_header(bad, subtitle=bad, icon="<b>")
    ui.hero(bad, subtitle=bad, eyebrow=bad, icon="<b>", badges=[{"text": bad, "icon": "<b>"}])
    ui.section_title(bad, icon="<b>", caption=bad)
    ui.stat_cards([{"label": bad, "value": bad, "icon": "<b>", "tone": "nope", "help": bad}])
    ui.empty_state(bad, bad, icon="<b>")
    ui.kv_list([(bad, bad), ("Vacío", ""), ("Nulo", None)])
    ui.feature_cards([{"title": bad, "text": bad, "icon": "<b>"}])
    ui.alert_list([{"title": bad, "text": bad, "tone": "warning", "icon": "<b>"}], label=bad)
    ui.quick_actions([{"page": None, "label": bad}])
    ui.footer(bad)


def test_every_component_escapes_user_text_when_rendered():
    at = AppTest.from_function(_render_all_script)
    at.run()
    assert not at.exception
    rendered = " ".join(m.value for m in at.markdown)
    _no_raw_xss(rendered)
    assert "<b>" not in rendered
    for cls in ("lab-page-header", "lab-hero", "lab-section__title", "lab-stat lab-stat--neutral",
                "lab-empty", "lab-kv", "lab-feature-grid", "lab-alert lab-alert--warning", "lab-footer"):
        assert cls in rendered
    assert rendered.count(">N/A<") == 2          # valores vacios y nulos


def test_quick_actions_without_pages_render_nothing():
    at = AppTest.from_function(_render_all_script)
    at.run()
    assert "lab-qa" not in " ".join(m.value for m in at.markdown)


# --- Contrato de style.css ------------------------------------------------------------

CONTRACT_CLASSES = [
    "lab-badge", "lab-section", "lab-section__title", "lab-section__caption", "lab-stat-grid", "lab-stat",
    "lab-stat__icon", "lab-stat__value", "lab-stat__label", "lab-stat__help", "lab-empty", "lab-empty__icon",
    "lab-empty__title", "lab-empty__text", "lab-timeline", "lab-step", "lab-step__marker", "lab-step__body",
    "lab-step__title", "lab-step__detail", "lab-step__time", "main-header", "page-subtitle", "user-chip",
    "lab-page-header", "lab-hero", "lab-card-head", "lab-kv", "lab-feature-grid", "lab-alert-list", "lab-qa",
]


@pytest.mark.parametrize("cls", CONTRACT_CLASSES)
def test_stylesheet_styles_every_contract_class(cls):
    assert re.search(rf"\.{re.escape(cls)}(?![\w-])", CSS), cls


@pytest.mark.parametrize("tone", ui.TONES)
def test_stylesheet_covers_every_tone(tone):
    assert f".lab-badge--{tone}" in CSS and f".lab-stat--{tone}" in CSS


@pytest.mark.parametrize("state", ui.STEP_STATES)
def test_stylesheet_covers_every_step_state_or_default(state):
    assert f".lab-step--{state}" in CSS or state == "pending" and ".lab-step__marker" in CSS


def test_stylesheet_keeps_dark_mode_with_both_signals():
    assert "@media (prefers-color-scheme: dark)" in CSS
    assert ':root:not([data-theme="light"])' in CSS
    assert ':root[data-theme="dark"]' in CSS


def test_stylesheet_respects_reduced_motion_and_disabled_buttons():
    assert "@media (prefers-reduced-motion: reduce)" in CSS
    assert ".stButton > button:disabled" in CSS
    assert "cursor: not-allowed" in CSS


def test_stylesheet_has_visible_focus_and_no_scripts_or_foreign_hosts():
    assert ":focus-visible" in CSS
    lowered = CSS.lower()
    assert "<script" not in lowered and "javascript:" not in lowered and "expression(" not in lowered
    hosts = set(re.findall(r"https?://([^/'\")]+)", CSS))
    assert hosts <= {"fonts.googleapis.com"}
