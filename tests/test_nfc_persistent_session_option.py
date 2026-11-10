# -*- coding: utf-8 -*-
"""nfc_sesion_persistente (mejoras/nfc-sesion-persistente).

Diagnostico (ver PR): el bug "cada toque NFC vuelve a pedir login" viene de que la cookie de
sesion es de SESION DEL NAVEGADOR (sin Max-Age: core/session.py, _emit_cookie) y de que la app
decide ya que hay que mostrar el login en la PRIMERA ejecucion de una pestana nueva, sin esperar
la respuesta del puente JS (core/session.py, cookie_from_browser) que lee la cookie en Streamlit
Community Cloud (app.py, bloque de restauracion).

Esta opcion (apagada por omision, igual patron que M1-M7 en core/options.py) agrega:
  1) Max-Age en la cookie (acotado por max_hours()) cuando se activa.
  2) Una espera acotada (unos pocos reintentos) antes de decidir que hace falta iniciar sesion.
  3) Una version global de los tokens (Secret SESSION_TOKEN_VERSION) para poder revocarlos todos
     de una vez, sin tocar el session_epoch de cada usuario.

Principio de diseno que se prueba explicitamente: el chip NFC identifica el ARTICULO (la URL
firmada con HMAC prueba que objeto es), nunca a la persona; solo la cookie/token de sesion
identifica a quien toca. Un toque SIN cookie valida nunca debe autenticar a nadie.

Pruebas puras (core/session.py, core/nfc.py) + un par de pruebas con streamlit.testing.v1.AppTest
para la parte que sí depende de Streamlit (la cookie que llega al navegador, la espera antes del
login). Ninguna prueba usa `streamlit run` ni rutas con separadores fijos: todo vive en memoria o
usa pathlib, por lo que corre igual en Windows."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core import nfc, options, session

SECRET = "frase-sintetica-solo-para-pruebas-de-sesion-persistente"
NOW = 1_800_000_000


def _dt(epoch):
    value = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return value


@pytest.fixture
def secrets(monkeypatch):
    """SESSION_SECRET configurado, como en tests/test_session_tokens.py. Los valores son sinteticos:
    no son el secreto real de ningun despliegue."""
    values = {"SESSION_SECRET": SECRET}
    monkeypatch.setattr(session, "safe_secret", lambda key, default=None: values.get(key, default))
    return values


@pytest.fixture
def sample_user():
    return {
        "id": "u-1", "session_epoch": 0, "status": "active", "role": "estudiante",
        "full_name": "Persona de Prueba", "institutional_email": "prueba@uniminuto.edu.co",
    }


class _VerifyStorage:
    """Lo minimo que session.verify_token necesita: un usuario y la opcion nfc_sesion_persistente."""

    def __init__(self, user, option_on=False):
        self._user = dict(user)
        self._settings = {options.NFC_SESSION_PERSISTENT: "1" if option_on else "0"}

    def get_setting(self, key, default=None):
        return self._settings.get(key, default)

    def set_setting(self, key, value, actor_email=""):
        self._settings[key] = value

    def get_user_by_id(self, user_id):
        return dict(self._user) if user_id == self._user["id"] else None

    def get_revoked_sessions(self, now=None):
        return {}

    def add_revoked_session(self, sid, exp, now=None):
        pass


# --- Firmar y verificar un token (lo que pide el punto 3 del encargo) --------------------------

def test_signing_a_token_includes_a_global_version_claim(secrets, sample_user):
    token, claims = session.issue_token(sample_user, now=_dt(NOW))
    assert token and "." in token
    assert claims["ver"] == 1          # SESSION_TOKEN_VERSION de omision


def test_a_valid_token_is_verified_and_restores_the_user(secrets, sample_user):
    storage = _VerifyStorage(sample_user)
    token, _ = session.issue_token(sample_user, now=_dt(NOW))
    restored, claims, reason = session.verify_token(token, storage, now=_dt(NOW + 5))
    assert reason is None
    assert restored["id"] == sample_user["id"]
    assert claims["ver"] == 1


def test_an_expired_token_is_rejected(secrets, sample_user):
    storage = _VerifyStorage(sample_user)
    token, claims = session.issue_token(sample_user, now=_dt(NOW))
    beyond_max_hours = claims["iat"] + session.max_hours() * 3600 + 1
    restored, restored_claims, reason = session.verify_token(token, storage, now=_dt(beyond_max_hours))
    assert restored is None and restored_claims is None
    assert "duración máxima" in reason


def test_an_idle_expired_token_is_also_rejected(secrets, sample_user):
    storage = _VerifyStorage(sample_user)
    token, _ = session.issue_token(sample_user, now=_dt(NOW))
    beyond_idle = NOW + session.idle_minutes() * 60 + 1
    restored, _, reason = session.verify_token(token, storage, now=_dt(beyond_idle))
    assert restored is None and "inactividad" in reason


def test_a_token_with_an_altered_signature_is_rejected(secrets, sample_user):
    storage = _VerifyStorage(sample_user)
    token, _ = session.issue_token(sample_user, now=_dt(NOW))
    body, signature = token.split(".")
    flipped = ("A" if signature[-1] != "A" else "B")
    tampered = f"{body}.{signature[:-1]}{flipped}"
    restored, claims, reason = session.verify_token(tampered, storage, now=_dt(NOW + 5))
    assert restored is None and claims is None and reason


def test_a_token_with_an_altered_body_is_rejected(secrets, sample_user):
    """El cuerpo se cambia (otro uid) pero se deja la firma original: lo que haria alguien que
    copio una cookie ajena intentando entrar como otra persona."""
    storage = _VerifyStorage(sample_user)
    token, _ = session.issue_token(sample_user, now=_dt(NOW))
    body, signature = token.split(".")
    payload = json.loads(session._unb64(body))
    payload["uid"] = "otro-usuario"
    forged_body = session._b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    forged = f"{forged_body}.{signature}"
    restored, claims, reason = session.verify_token(forged, storage, now=_dt(NOW + 5))
    assert restored is None and claims is None and reason


def test_a_token_from_a_revoked_global_version_is_rejected_only_with_the_option_on(secrets, sample_user):
    """El punto central del "kid"/version: subir SESSION_TOKEN_VERSION revoca TODOS los tokens
    anteriores de una vez, pero SOLO si nfc_sesion_persistente esta activa (si no, el comportamiento
    de siempre no cambia, como exige el encargo)."""
    token, _ = session.issue_token(sample_user, now=_dt(NOW))      # emitido con version 1 (de omision)
    secrets["SESSION_TOKEN_VERSION"] = 2                            # el administrador revoca todo

    storage_on = _VerifyStorage(sample_user, option_on=True)
    restored_on, _, reason_on = session.verify_token(token, storage_on, now=_dt(NOW + 5))
    assert restored_on is None and "revocada" in reason_on

    storage_off = _VerifyStorage(sample_user, option_on=False)
    restored_off, _, reason_off = session.verify_token(token, storage_off, now=_dt(NOW + 5))
    assert restored_off is not None and reason_off is None


def test_a_fresh_token_after_the_version_bump_is_accepted_with_the_option_on(secrets, sample_user):
    secrets["SESSION_TOKEN_VERSION"] = 2
    token, claims = session.issue_token(sample_user, now=_dt(NOW))
    assert claims["ver"] == 2
    storage_on = _VerifyStorage(sample_user, option_on=True)
    restored, _, reason = session.verify_token(token, storage_on, now=_dt(NOW + 5))
    assert restored is not None and reason is None


def test_legacy_tokens_without_the_version_claim_still_work_by_default(secrets, sample_user):
    """Compatibilidad: un token de ANTES de este cambio (sin "ver") sigue sirviendo si la opcion
    esta apagada, que es justo el requisito de no romper nada por omision."""
    storage_off = _VerifyStorage(sample_user, option_on=False)
    token, _ = session.issue_token(sample_user, now=_dt(NOW))
    body, signature = token.split(".")
    payload = json.loads(session._unb64(body))
    payload.pop("ver", None)
    legacy_body = session._b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    legacy_token = f"{legacy_body}.{session._sign(legacy_body, SECRET)}"
    restored, _, reason = session.verify_token(legacy_token, storage_off, now=_dt(NOW + 5))
    assert restored is not None and reason is None


# --- El toque NFC nunca autentica: solo navega al articulo (causa (d) del diagnostico, descartada,
#     y requisito explicito del encargo) ----------------------------------------------------------

def test_an_nfc_tap_without_a_valid_cookie_never_authenticates_anyone(storage):
    """`storage` es el FakeStorage de tests/conftest.py. Sin sesion restaurada, `user` es None, como
    si la cookie no existiera o no fuera valida: handle_tap debe registrar el toque sobre el
    ARTICULO (prueba de que alguien estuvo frente a el) pero jamas debe crear un usuario, una
    sesion ni nada parecido a una identidad -- esa responsabilidad es solo de la cookie/token."""
    item = storage.add_item("2-1-01-01-001", name="Arduino UNO", item_type="child", quantity=3)
    outcome = nfc.handle_tap(storage, None, item["id"])
    assert outcome["ok"] is True                   # el chip SI identifico el articulo
    assert outcome["target"] == nfc.TARGET_SCAN     # sin persona no hay conteo ni ruta a los que ir
    events = storage.get_trace_events(event_type="nfc_tap")
    assert len(events) == 1
    assert events[0]["user_id"] == "" and events[0]["actor_id"] == ""   # ningun usuario quedo autenticado


def test_an_nfc_tap_does_not_depend_on_the_session_module_at_all():
    """`handle_tap` no conoce `core.session`: ni lo importa. Un toque de chip es, estructuralmente,
    independiente de quien haya iniciado sesion (el principio del encargo: el chip identifica el
    ARTICULO; la cookie/token identifica a la PERSONA, en otro modulo que nfc.py ni toca)."""
    import core.nfc as nfc_module

    source = Path(nfc_module.__file__).read_text(encoding="utf-8")
    assert "import session" not in source and "core.session" not in source


# --- Cookie persistente: Max-Age acotado solo cuando la opcion esta encendida -------------------

pytest.importorskip("streamlit.testing.v1")

from streamlit.testing.v1 import AppTest  # noqa: E402


def _cookie_bridge_script():
    import streamlit as st

    from core import session

    session.render_cookie_command()
    step = st.session_state.get("step", "")
    if step == "persistent":
        st.session_state.step = ""
        session.queue_cookie("token-de-prueba", persistent=True)
        st.rerun()
    if step == "default":
        st.session_state.step = ""
        session.queue_cookie("token-de-prueba")          # persistent=False, el valor de omision
        st.rerun()
    if step == "clear_persistent":
        st.session_state.step = ""
        session.queue_cookie(clear=True, persistent=True)  # un borrado siempre es Max-Age=0, aunque pida persistent
        st.rerun()
    session.finish_cookie_command()


def _cookie_writes(at) -> list:
    return [element.proto.srcdoc for element in at.get("iframe")
            if session.COOKIE_NAME in getattr(element.proto, "srcdoc", "")]


def test_a_persistent_cookie_write_includes_a_max_age_tied_to_max_hours(secrets):
    at = AppTest.from_function(_cookie_bridge_script)
    at.session_state["step"] = "persistent"
    at.run()
    writes = _cookie_writes(at)
    assert writes and f"Max-Age={session.max_hours() * 3600}" in writes[0]


def test_the_default_write_still_has_no_max_age_like_before_this_change(secrets):
    """Con `persistent` sin pedir (el valor por omision, False), la cookie sigue siendo de sesion
    del navegador: el mismo comportamiento (bug incluido) de antes de esta opcion."""
    at = AppTest.from_function(_cookie_bridge_script)
    at.session_state["step"] = "default"
    at.run()
    writes = _cookie_writes(at)
    assert writes and "Max-Age" not in writes[0]


def test_clearing_a_persistent_cookie_still_uses_max_age_zero():
    """Borrar (cerrar sesion) siempre usa Max-Age=0, sin importar `persistent`: lo que importa para
    cerrar sesion es borrar la cookie ya, no acortar cuanto dura una que se esta escribiendo."""
    at = AppTest.from_function(_cookie_bridge_script)
    at.session_state["step"] = "clear_persistent"
    at.run()
    writes = _cookie_writes(at)
    assert writes and "Max-Age=0" in writes[0]


# --- Esperar (acotado) la cookie del navegador antes de pedir login (causa (c) del diagnostico) --
#
# Reproduce, en un script minimo, el mismo bloque que app.py agrega en la restauracion (ver PR):
# sin la opcion, un `cookie_from_browser()` que todavia no contesto (None) cae directo al "login";
# con la opcion, se reintenta un numero acotado de veces (session.RESTORE_WAIT_LIMIT) antes de
# darse por vencido. No se usa app.py completo (pediria sembrar usuarios/items): esto prueba el
# control de flujo real con las constantes reales de core/session.py.

def _wait_script():
    import streamlit as st

    from core import options, session

    storage = st.session_state["storage"]
    if not st.session_state.get("user") and not st.session_state.get(session.RESTORE_DONE_KEY):
        saved_token = session.cookie_from_context() or session.cookie_from_browser()
        if saved_token is not None:
            st.session_state[session.RESTORE_DONE_KEY] = True
        if saved_token:
            st.session_state["user"] = {"id": "restored"}
        elif saved_token is None and options.enabled(storage, options.NFC_SESSION_PERSISTENT):
            tries = int(st.session_state.get(session.RESTORE_WAIT_KEY, 0)) + 1
            st.session_state[session.RESTORE_WAIT_KEY] = tries
            if tries <= session.RESTORE_WAIT_LIMIT:
                st.rerun()
    st.session_state["calls"] = st.session_state.get("calls", 0) + 1
    if not st.session_state.get("user"):
        st.text_input("Contraseña")           # lo que se ve como "pantalla de login"


def _flag_storage(option_on):
    class _Storage:
        def get_setting(self, key, default=None):
            return "1" if option_on else "0"

    return _Storage()


def test_without_the_option_one_unanswered_browser_read_shows_the_login_right_away(monkeypatch):
    from core import session

    monkeypatch.setattr(session, "cookie_from_context", lambda: "")
    monkeypatch.setattr(session, "cookie_from_browser", lambda: None)   # el puente nunca contesta en esta prueba
    at = AppTest.from_function(_wait_script)
    at.session_state["storage"] = _flag_storage(option_on=False)
    at.run()
    assert len(at.text_input) == 1 and at.text_input[0].label == "Contraseña"   # "login" de una vez


def test_with_the_option_bounded_retries_give_the_slow_cookie_a_chance(monkeypatch):
    from core import session

    reads = {"n": 0}

    def browser():
        reads["n"] += 1
        return None if reads["n"] <= session.RESTORE_WAIT_LIMIT else "token-de-prueba"

    monkeypatch.setattr(session, "cookie_from_context", lambda: "")
    monkeypatch.setattr(session, "cookie_from_browser", browser)
    at = AppTest.from_function(_wait_script)
    at.session_state["storage"] = _flag_storage(option_on=True)
    at.run()
    assert not at.text_input                           # el login nunca se mostro
    assert at.session_state["user"] == {"id": "restored"}
    assert reads["n"] == session.RESTORE_WAIT_LIMIT + 1  # se esperaron los intentos justos, ni uno de mas


def test_with_the_option_but_a_bridge_that_never_answers_still_gives_up_and_shows_login(monkeypatch):
    from core import session

    monkeypatch.setattr(session, "cookie_from_context", lambda: "")
    monkeypatch.setattr(session, "cookie_from_browser", lambda: None)   # nunca contesta
    at = AppTest.from_function(_wait_script)
    at.session_state["storage"] = _flag_storage(option_on=True)
    at.run()
    assert at.text_input and at.text_input[0].label == "Contraseña"     # se agotaron los intentos: login
    # El ultimo intento (el que supera el limite y por eso ya no reintenta) tambien queda contado.
    assert at.session_state[session.RESTORE_WAIT_KEY] == session.RESTORE_WAIT_LIMIT + 1
