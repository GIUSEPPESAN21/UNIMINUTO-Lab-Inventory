"""M4 · autenticidad y trazabilidad de chips NFC: rotación de clave (kid), espejo NTAG21x y salud de chips."""

from datetime import datetime, timedelta, timezone

import pytest

from core import nfc, options, traceability

BASE = "https://lab.example.edu.co"
CODE = "2-1-01-00-000"
OLD_KEY, NEW_KEY = "clave-sintetica-vieja", "clave-sintetica-nueva"
PROF = {"id": "p1", "full_name": "Profe", "institutional_email": "profe@ludica.invalid", "role": "profesor"}
ADMIN = {"id": "m1", "full_name": "Admin", "institutional_email": "admin@ludica.invalid", "role": "maestro"}
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
UID = "04A1B2C3D4E5F6"


def _mirror(counter: int, uid: str = UID) -> str:
    return f"{uid}x{counter:06X}"


@pytest.fixture
def lab(storage):
    storage.add_item(CODE, name="Electrónica", item_type="master")
    return storage


@pytest.fixture
def rotated(monkeypatch):
    secrets = {"NFC_SECRET": OLD_KEY, "NFC_KEYS": {"2": NEW_KEY}, "NFC_KID": "2"}
    monkeypatch.setattr(nfc, "safe_secret", lambda key, default=None: secrets.get(key, default))
    return secrets


def _audit(storage, strict=False):
    assert options.set_enabled(storage, ADMIN, options.NFC_AUDIT, True)[0]
    if strict:
        assert options.set_enabled(storage, ADMIN, options.NFC_STRICT, True)[0]


def _tap(storage, counter=None, when=T0, uid=UID, signature=None, kid="", user=PROF):
    signature = nfc.sign_code(CODE, OLD_KEY) if signature is None else signature
    mirror = _mirror(counter, uid) if counter is not None else ""
    return nfc.handle_tap(storage, user, CODE, signature, secret=OLD_KEY, now=when, kid=kid, mirror=mirror)


# --- Valores por defecto: igual que antes -----------------------------------------------------

def test_default_url_and_parse_are_unchanged():
    assert nfc.build_tag_url(CODE, BASE, OLD_KEY) == f"{BASE}/escanear?nfc={CODE}&s={nfc.sign_code(CODE, OLD_KEY)}"
    assert nfc.build_tag_url(CODE, BASE) == f"{BASE}/escanear?nfc={CODE}"
    assert nfc.parse_tap({"nfc": CODE, "s": "ABC"}) == {"code": CODE, "signature": "ABC", "error": ""}


def test_default_ignores_the_mirror_and_records_no_rejections(lab):
    first = _tap(lab, 5)
    again = _tap(lab, 5, T0 + timedelta(minutes=5))                    # contador repetido: sin auditoría no importa
    assert first["ok"] and again["ok"] and "anomalies" not in again
    assert all("mirror" not in traceability.event_details(e) for e in lab.trace_events)
    bad = _tap(lab, signature="NOVALIDA00")
    assert not bad["ok"] and bad["signature"] == nfc.SIG_INVALID
    assert not lab.get_trace_events(event_type=traceability.EVENT_NFC_TAP_REJECTED)


# --- Rotación de clave -------------------------------------------------------------------------

def test_new_tags_carry_the_active_key_version(rotated):
    kid, secret = nfc.signing_key()
    assert (kid, secret) == ("2", NEW_KEY)
    url = nfc.build_tag_url(CODE, BASE, secret, kid=kid, mirror=True)
    assert url == (f"{BASE}/escanear?nfc={CODE}&s={nfc.sign_code(CODE, NEW_KEY)}&k=2"
                   f"&m={nfc.MIRROR_PLACEHOLDER}")
    assert nfc.parse_tap({"nfc": CODE, "s": "X", "k": "2", "m": "AB"}) == {
        "code": CODE, "signature": "X", "error": "", "kid": "2", "mirror": "AB"}


def test_old_and_new_tags_validate_and_unknown_versions_are_rejected(lab, rotated):
    assert nfc.handle_tap(lab, PROF, CODE, nfc.sign_code(CODE, OLD_KEY), now=T0)["ok"]           # chip viejo, sin k
    later = T0 + timedelta(minutes=2)
    assert nfc.handle_tap(lab, PROF, CODE, nfc.sign_code(CODE, NEW_KEY), now=later, kid="2")["ok"]
    unknown = nfc.handle_tap(lab, PROF, CODE, nfc.sign_code(CODE, NEW_KEY), now=later, kid="9")
    assert not unknown["ok"] and unknown["signature"] == nfc.SIG_UNKNOWN_KEY
    swapped = nfc.handle_tap(lab, PROF, CODE, nfc.sign_code(CODE, OLD_KEY), now=later, kid="2")
    assert not swapped["ok"] and swapped["signature"] == nfc.SIG_INVALID


# --- Espejo NTAG21x ----------------------------------------------------------------------------

def test_parse_mirror_formats():
    assert nfc.parse_mirror(_mirror(26)) == {"uid": UID, "counter": 26, "configured": True}
    assert nfc.parse_mirror("00001A") == {"uid": "", "counter": 26, "configured": True}
    assert nfc.parse_mirror(nfc.MIRROR_PLACEHOLDER)["configured"] is False
    assert nfc.parse_mirror("no-es-espejo") == {} and nfc.parse_mirror("") == {}


def test_counter_that_does_not_grow_is_an_alert_recorded_with_the_tap(lab):
    _audit(lab)
    assert _tap(lab, 5)["ok"]
    replay = _tap(lab, 5, T0 + timedelta(seconds=10))        # misma URL: el contador no creció
    assert replay["ok"] and replay["tone"] == "warning" and nfc.ANOMALY_COUNTER in replay["anomalies"]
    assert "Alerta registrada" in replay["message"]
    details = traceability.event_details(replay["event"])
    assert details["anomalies"] == [nfc.ANOMALY_COUNTER] and details["mirror"]["counter"] == 5
    assert _tap(lab, 6, T0 + timedelta(minutes=3))["ok"] and "anomalies" not in _tap(lab, 7, T0 + timedelta(minutes=9))


def test_strict_mode_blocks_and_records_the_anomaly(lab):
    _audit(lab, strict=True)
    assert _tap(lab, 5)["ok"]
    blocked = _tap(lab, 4, T0 + timedelta(minutes=2))
    assert not blocked["ok"] and "bloqueado" in blocked["message"]
    rejected = lab.get_trace_events(event_type=traceability.EVENT_NFC_TAP_REJECTED)
    assert len(rejected) == 1 and traceability.event_details(rejected[0])["reason"] == nfc.REJECT_ANOMALY
    assert len(lab.get_trace_events(event_type=traceability.EVENT_NFC_TAP)) == 1   # el toque bloqueado no cuenta


def test_other_uid_and_missing_mirror_are_anomalies(lab):
    _audit(lab)
    assert _tap(lab, 5)["ok"]
    clone = _tap(lab, 9, T0 + timedelta(minutes=2), uid="04FFFFFFFFFFFF")
    assert nfc.ANOMALY_UID in clone["anomalies"]
    edited = _tap(lab, None, T0 + timedelta(minutes=4))     # alguien quitó &m= de la URL
    assert edited["anomalies"] == [nfc.ANOMALY_MISSING]


def test_audit_records_invalid_signatures_once_per_minute(lab):
    _audit(lab)
    for seconds in (0, 20):
        assert not _tap(lab, signature="NOVALIDA00", when=T0 + timedelta(seconds=seconds))["ok"]
    rejected = lab.get_trace_events(event_type=traceability.EVENT_NFC_TAP_REJECTED)
    assert len(rejected) == 1 and traceability.event_details(rejected[0])["reason"] == nfc.SIG_INVALID


# --- Salud de chips ----------------------------------------------------------------------------

def test_chip_health_summarises_reads_alerts_duplicates_and_stale_chips(lab):
    nfc.register_tag(lab, "2-1-02-00-000", ADMIN, chip="NTAG215", now=T0 - timedelta(days=60))
    _audit(lab)
    assert _tap(lab, 5)["ok"]
    _tap(lab, 5, T0 + timedelta(minutes=1))                                   # alerta de contador
    _tap(lab, 8, T0 + timedelta(minutes=2), uid="04FFFFFFFFFFFF")            # otro UID: duplicado
    _tap(lab, signature="NOVALIDA00", when=T0 + timedelta(minutes=3))        # firma inválida
    rows = {row["code"]: row for row in nfc.chip_health(lab, now=T0 + timedelta(hours=1))}
    chip = rows[CODE]
    assert chip["taps"] == 3 and chip["counter_alerts"] == 1 and chip["uid_alerts"] == 1
    assert chip["distinct_uids"] == 2 and chip["duplicated"] and chip["invalid_signature"] == 1
    assert not chip["stale"]
    assert rows["2-1-02-00-000"]["stale"] and rows["2-1-02-00-000"]["taps"] == 0


def test_only_the_admin_turns_on_the_nfc_options(storage):
    with pytest.raises(PermissionError):
        options.set_enabled(storage, PROF, options.NFC_AUDIT, True)
    assert not nfc.audit_enabled(storage) and not nfc.strict_enabled(storage)
    assert options.set_enabled(storage, ADMIN, options.NFC_STRICT, True)[0]
    assert not nfc.strict_enabled(storage)               # sin la auditoría, el modo estricto no actúa
