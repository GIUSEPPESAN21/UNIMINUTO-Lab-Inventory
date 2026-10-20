# -*- coding: utf-8 -*-
"""Etiquetas de rol de v1.15: Cliente, Operario, Supervisor y Administrador viven solo en
ROLE_LABELS; las etiquetas anteriores ya no aparecen en la interfaz, los correos ni la documentación."""

import re
from pathlib import Path

import pytest

pytest.importorskip("streamlit.testing.v1")

from streamlit.testing.v1 import AppTest  # noqa: E402

from core import permissions  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OLD = ("Encargado de despacho", "Encargado del laboratorio", "Perfil maestro")


def test_labels_and_technical_ids():
    assert permissions.ROLES == ("estudiante", "operario", "encargado", "profesor", "maestro")
    assert permissions.ROLE_LABELS == {
        "estudiante": "Cliente", "operario": "Operario", "encargado": "Supervisor",
        "profesor": "Supervisor", "maestro": "Administrador",
    }
    assert permissions.role_label({"role": "profesor"}) == permissions.role_label("encargado") == "Supervisor"
    assert permissions.role_label("desconocido") == "desconocido" and permissions.role_label("") == "Sin rol"


def test_users_page_tells_the_two_supervisors_apart():
    assert permissions.role_detail_label("encargado") == "Supervisor (encargado)"
    assert permissions.role_detail_label("profesor") == "Supervisor (profesor)"
    assert permissions.role_detail_label("estudiante") == "Cliente"
    assert len({permissions.role_detail_label(role) for role in permissions.ROLES}) == 5


def _sources():
    for folder in ("core", "views"):
        yield from (ROOT / folder).glob("*.py")
    yield ROOT / "app.py"


def test_old_labels_are_gone_from_the_interface_code():
    offenders = []
    for path in _sources():
        if path.name == "permissions.py":
            continue
        text = path.read_text(encoding="utf-8")
        for old in OLD:
            if old in text:
                offenders.append(f"{path.relative_to(ROOT)}: {old}")
        if re.search(r"""["']Estudiante["']""", text):
            offenders.append(f"{path.relative_to(ROOT)}: «Estudiante» como etiqueta")
    assert offenders == []


def test_readme_documents_the_four_roles_without_the_old_labels():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    roles = readme[readme.index("## Roles"):readme.index("**Seguridad del registro:**")]
    for label in ("**Cliente**", "**Operario**", "**Supervisor**", "**Administrador**"):
        assert label in roles
    for old in OLD:
        assert old not in readme


def _page_script():
    import importlib

    import streamlit as st

    from core.storage import LabStorage

    st.session_state.storage = LabStorage()
    st.session_state.user = {"id": "u1", "role": st.session_state.test_role, "full_name": "Persona Prueba",
                             "institutional_email": "p@uniminuto.edu.co", "program_or_department": "Ingeniería"}
    importlib.import_module(f"views.{st.session_state.test_view}").render()


def _open(view, role="maestro"):
    at = AppTest.from_function(_page_script, default_timeout=60)
    at.session_state["test_role"], at.session_state["test_view"] = role, view
    at.run()
    assert not at.exception
    return at


def test_about_page_lists_the_four_roles():
    html = " ".join(str(e.value) for e in _open("acerca_de").markdown)
    for label in ("Cliente", "Operario", "Supervisor", "Administrador"):
        assert label in html
    for old in OLD:
        assert old not in html


@pytest.mark.parametrize("role,label", [("estudiante", "Cliente"), ("operario", "Operario"),
                                        ("encargado", "Supervisor"), ("profesor", "Supervisor"),
                                        ("maestro", "Administrador")])
def test_home_shows_the_label_of_the_role_and_the_role_guide(role, label):
    at = _open("inicio", role)
    html = " ".join(str(e.value) for e in at.markdown)
    assert label in html
    guide = " ".join(str(e.value) for e in at.markdown if "Roles:" in str(e.value))
    for name in ("Cliente", "Operario", "Supervisor", "Administrador"):
        assert name in guide
