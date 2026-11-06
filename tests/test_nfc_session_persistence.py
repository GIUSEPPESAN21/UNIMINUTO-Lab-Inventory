"""Toques NFC con sesión persistente (core/session.py + app.py).

Cada toque de un chip abre una pestaña NUEVA del navegador (otra sesión de Streamlit). Se inicia sesión una
sola vez, en el primer toque; los siguientes toques llegan con la cookie de sesión y van directo a la acción
(conteo, picking…) registrando la trazabilidad, sin volver a mostrar el inicio de sesión."""

import json
from pathlib import Path

import pytest

pytest.importorskip("streamlit.testing.v1")

from streamlit.testing.v1 import AppTest  # noqa: E402

from core import auth, dispatch, inventory_count, picking, service_requests, session, traceability  # noqa: E402

APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")
PASSWORD = "ClaveSegura123"
SECRET = "frase-sintetica-solo-para-pruebas-de-sesion"
CONTAINER, FIRST, SECOND, THIRD = "2-1-01-00-000", "2-1-01-01-001", "2-1-01-01-002", "2-1-01-01-003"


@pytest.fixture
def persistent(monkeypatch):
    """SESSION_SECRET configurado (sin él la sesión no sobrevive a una pestaña nueva, por diseño)."""
    values = {"SESSION_SECRET": SECRET}
    monkeypatch.setattr(session, "safe_secret", lambda key, default=None: values.get(key, default))
    return monkeypatch


def _tab(code):
    """Una pestaña nueva del navegador, abierta por el toque de un chip (?nfc=código)."""
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.query_params["nfc"] = code
    at.run()
    assert not at.exception
    return at


def _login(at, email):
    at.text_input[0].input(email)
    at.text_input[1].input(PASSWORD)
    at.button[0].click().run()
    assert not at.exception


def _login_screen(at) -> bool:
    return any(widget.label == "Contraseña" for widget in at.text_input)


def _cookie_writes(at) -> list:
    """Escrituras de la cookie de sesión que recibe el navegador en la ejecución final."""
    return [element.proto.srcdoc for element in at.get("iframe")
            if session.COOKIE_NAME in getattr(element.proto, "srcdoc", "")]


def _seed_items(storage):
    storage.save_item({"name": "Electrónica", "item_type": "master"}, CONTAINER, is_new=True)
    for code, name in ((FIRST, "Arduino UNO"), (SECOND, "Protoboard"), (THIRD, "Sensor")):
        storage.save_item({"name": name, "item_type": "child", "parent_id": CONTAINER, "quantity": 3}, code,
                          is_new=True)


def _user(storage, email, role):
    return storage.create_user(f"Persona {role}", email, auth.hash_password(PASSWORD), role)


def _logins(storage) -> int:
    return len(storage.get_trace_events(event_type=traceability.EVENT_SESSION_LOGIN))


def _count_taps(storage, count_id) -> list:
    return [event["item_id"] for event in storage.get_trace_events(event_type=traceability.EVENT_NFC_TAP)
            if traceability.event_details(event).get("count_session") == count_id]


def _restore_with(monkeypatch, at):
    """Lo que hace el navegador: guarda la cookie que escribió la pestaña y la manda en las pestañas nuevas."""
    token = at.session_state["session_token"]
    writes = _cookie_writes(at)
    assert token and writes and all(json.dumps(token) in write for write in writes)
    monkeypatch.setattr(session, "cookie_from_context", lambda: token)


def test_first_tap_logs_in_once_and_next_taps_go_straight_to_the_count(persistent):
    first = _tab(FIRST)
    storage = first.session_state["storage"]
    _seed_items(storage)
    prof = _user(storage, "prof@uniminuto.edu.co", "profesor")
    ok, _, count = inventory_count.start_session(storage, prof, "Conteo de prueba")
    assert ok
    assert _login_screen(first)                                  # primer toque: se inicia sesión una vez
    _login(first, "prof@uniminuto.edu.co")
    assert first.session_state["user"]["id"] == prof["id"]
    assert _count_taps(storage, count["id"]) == [FIRST]          # el toque que esperaba quedó en el conteo
    _restore_with(persistent, first)                             # la cookie llegó pese a ir a la página del conteo

    for code in (SECOND, THIRD):                                 # toques siguientes: pestañas nuevas
        tab = _tab(code)
        assert not _login_screen(tab)
        assert tab.session_state["user"]["id"] == prof["id"]
    assert _count_taps(storage, count["id"]) == [FIRST, SECOND, THIRD]
    assert _logins(storage) == 1


def test_picking_taps_reach_the_open_picking_without_logging_in_again(persistent):
    first = _tab(CONTAINER)
    storage = first.session_state["storage"]
    _seed_items(storage)
    client = _user(storage, "cliente@uniminuto.edu.co", "estudiante")
    supervisor = _user(storage, "super@uniminuto.edu.co", "encargado")
    operator = _user(storage, "operario@uniminuto.edu.co", "operario")
    request, _, _ = service_requests.submit_request(storage, client, "product", FIRST, 1)
    assert service_requests.review_request(storage, request["id"], "approved", supervisor)[0]
    assert dispatch.claim_request(storage, request["id"], operator)[0]
    assert dispatch.start_picking(storage, request["id"], operator)[0]

    def activity() -> int:
        return sum(len(rows(operator["id"], request["id"]))
                   for rows in (picking.readings, picking.failed, picking.route_steps))

    _login(first, "operario@uniminuto.edu.co")
    assert activity() == 1                                       # el toque se sumó al picking abierto
    _restore_with(persistent, first)
    second = _tab(FIRST)
    assert not _login_screen(second) and second.session_state["user"]["id"] == operator["id"]
    assert activity() == 2
    assert _logins(storage) == 1


def test_without_session_secret_each_new_tab_still_asks_for_login():
    """Sin SESSION_SECRET no hay cookie: comportamiento de siempre (y el administrador ve el aviso)."""
    first = _tab(FIRST)
    storage = first.session_state["storage"]
    _seed_items(storage)
    _user(storage, "prof@uniminuto.edu.co", "profesor")
    _login(first, "prof@uniminuto.edu.co")
    assert not first.session_state["session_token"] and _cookie_writes(first) == []
    second = _tab(SECOND)
    assert _login_screen(second) and second.session_state["user"] is None
    assert any("Tocaste el chip NFC" in element.value for element in second.info)


# --- Streamlit Community Cloud: la cookie no llega al servidor --------------------------------

def _cloud_browser(monkeypatch, token=""):
    """Community Cloud filtra las cookies de la app antes del servidor (st.context.cookies vacío); solo el
    navegador las ve. En la primera ejecución de cada pestaña el puente todavía no respondió (None); su
    respuesta, con la cookie que guarda el navegador, provoca la ejecución siguiente."""
    def browser():
        import streamlit as st
        reads = st.session_state.get("_bridge_reads", 0) + 1
        st.session_state["_bridge_reads"] = reads
        return None if reads == 1 else token

    monkeypatch.setattr(session, "cookie_from_context", lambda: "")
    monkeypatch.setattr(session, "cookie_from_browser", browser)


def _browser_answers(tab):
    """El navegador responde: la app se vuelve a ejecutar en la misma pestaña."""
    tab.run()
    assert not tab.exception


def test_on_community_cloud_the_browser_cookie_keeps_the_session_between_taps(persistent):
    _cloud_browser(persistent)                                   # primer toque: el navegador no tiene cookie
    first = _tab(FIRST)
    storage = first.session_state["storage"]
    _seed_items(storage)
    prof = _user(storage, "prof@uniminuto.edu.co", "profesor")
    ok, _, count = inventory_count.start_session(storage, prof, "Conteo de prueba")
    assert ok and _login_screen(first)
    _login(first, "prof@uniminuto.edu.co")
    token = first.session_state["session_token"]
    writes = _cookie_writes(first)
    assert token and writes and all(json.dumps(token) in write for write in writes)
    _cloud_browser(persistent, token)                            # el navegador guardó esa cookie

    for code in (SECOND, THIRD):                                 # toques siguientes: pestañas nuevas
        tab = _tab(code)
        _browser_answers(tab)
        assert not _login_screen(tab)
        assert tab.session_state["user"]["id"] == prof["id"]
    assert _count_taps(storage, count["id"]) == [FIRST, SECOND, THIRD]
    assert _logins(storage) == 1


def test_a_tab_reads_the_browser_cookie_only_once(persistent):
    """Tras cerrar sesión, la pestaña no vuelve a leer la cookie: una sesión cerrada no revive."""
    _cloud_browser(persistent)
    first = _tab(FIRST)
    storage = first.session_state["storage"]
    _seed_items(storage)
    prof = _user(storage, "prof@uniminuto.edu.co", "profesor")
    _login(first, "prof@uniminuto.edu.co")
    _cloud_browser(persistent, first.session_state["session_token"])
    tab = _tab(SECOND)
    _browser_answers(tab)
    assert tab.session_state["user"]["id"] == prof["id"] and tab.session_state["_bridge_reads"] == 2
    next(b for b in tab.sidebar.button if "Cerrar sesión" in b.label).click().run()
    assert not tab.exception and _login_screen(tab) and tab.session_state["user"] is None
    assert tab.session_state["_bridge_reads"] == 2


@pytest.mark.parametrize("answer, expected", [
    (None, None),                          # el navegador aún no respondió
    ({"token": "abc.def"}, "abc.def"),     # el navegador tiene la cookie
    ({"token": ""}, ""),                   # no hay cookie
    ("inesperado", ""),                    # respuesta desconocida: se pide el inicio de sesión
])
def test_browser_bridge_answers(monkeypatch, answer, expected):
    monkeypatch.setattr(session, "_bridge", lambda **kwargs: answer)
    assert session.cookie_from_browser() == expected


def test_a_broken_bridge_falls_back_to_the_login(monkeypatch):
    def broken(**kwargs):
        raise RuntimeError("sin componente")

    monkeypatch.setattr(session, "_bridge", broken)
    assert session.cookie_from_browser() == ""


def test_the_bridge_page_ships_with_the_app():
    page = (session._BRIDGE_DIR / "index.html").read_text(encoding="utf-8")
    assert "streamlit:componentReady" in page and "streamlit:setComponentValue" in page
    assert "cookie_name" in page


# --- El puente de cookie, aislado ------------------------------------------------------------

def _bridge_script():
    import streamlit as st

    from core import session

    session.render_cookie_command()
    step = st.session_state.get("step", "")
    if step == "login":
        st.session_state.step = "switch"
        session.queue_cookie("token-de-prueba")
        st.rerun()                                     # como el inicio de sesión
    if step == "switch":
        st.session_state.step = ""
        st.rerun()                                     # como st.switch_page tras procesar el toque
    if step == "logout":
        st.session_state.step = ""
        session.queue_cookie(clear=True)
        st.rerun()
    session.finish_cookie_command()


def test_a_cookie_write_survives_a_page_switch_and_stops_once_confirmed():
    at = AppTest.from_function(_bridge_script)
    at.session_state["step"] = "login"
    at.run()
    assert not at.exception
    assert any('"token-de-prueba"' in write for write in _cookie_writes(at))
    assert "session_cookie_command" not in at.session_state      # una ejecución completa la confirmó
    at.run()
    assert _cookie_writes(at) == []                              # no se repite sin necesidad


def test_clearing_the_cookie_is_sent_once():
    at = AppTest.from_function(_bridge_script)
    at.session_state["step"] = "logout"
    at.run()
    writes = _cookie_writes(at)
    assert len(writes) == 1 and "Max-Age=0" in writes[0]
    at.run()
    assert _cookie_writes(at) == []
