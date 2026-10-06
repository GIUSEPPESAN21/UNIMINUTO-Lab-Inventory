# -*- coding: utf-8 -*-
"""Notificaciones opcionales por WhatsApp (Twilio) y correo SMTP.

Un fallo de proveedor nunca invalida la operación principal: las solicitudes
se guardan primero y la interfaz informa si la notificación no pudo enviarse.
Las credenciales solo se leen desde st.secrets y nunca se registran.
"""

import logging
import re
import smtplib
import ssl
import threading
from email.message import EmailMessage

from core.config import safe_secret

logger = logging.getLogger(__name__)

WHATSAPP_SECRETS = [
    "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
    "TWILIO_WHATSAPP_FROM_NUMBER", "DESTINATION_WHATSAPP_NUMBER",
]
_EMAIL_SPLIT_RE = re.compile(r"[,;\s]+")


def _as_bool(value, default=False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "si", "sí", "on")


def _clean_email(value: str) -> str:
    value = (value or "").strip().lower()
    if "\n" in value or "\r" in value or value.count("@") != 1:
        return ""
    return value


def _configured_admin_emails() -> list:
    raw = safe_secret("ADMIN_NOTIFICATION_EMAILS", "")
    values = raw if isinstance(raw, (list, tuple)) else _EMAIL_SPLIT_RE.split(str(raw or ""))
    return sorted({_clean_email(v) for v in values if _clean_email(v)})


def get_admin_notification_emails(storage=None) -> list:
    """Destinatarios explícitos o, como respaldo, profesores/maestros activos."""
    configured = _configured_admin_emails()
    if configured:
        return configured
    if not storage:
        return []
    try:
        return sorted({
            _clean_email(user.get("institutional_email", ""))
            for user in storage.get_all_users()
            if user.get("status") == "active"
            and user.get("role") in ("profesor", "maestro")
            and _clean_email(user.get("institutional_email", ""))
        })
    except Exception as exc:
        logger.warning(f"No se pudieron obtener destinatarios administrativos: {exc}")
        return []


def send_email_notification(subject: str, body: str, recipients: list) -> tuple:
    """Envía texto plano por SMTP. Devuelve (ok, mensaje) y nunca propaga."""
    recipients = sorted({_clean_email(v) for v in (recipients or []) if _clean_email(v)})
    if not recipients:
        return False, "No hay correos administrativos configurados o activos."

    host = str(safe_secret("SMTP_HOST", "") or "").strip()
    username = str(safe_secret("SMTP_USERNAME", "") or "").strip()
    password = str(safe_secret("SMTP_PASSWORD", "") or "")
    from_email = _clean_email(str(safe_secret("SMTP_FROM_EMAIL", username) or username))
    try:
        port = int(safe_secret("SMTP_PORT", 587) or 587)
    except (TypeError, ValueError):
        return False, "SMTP_PORT debe ser un número entero."
    use_ssl = _as_bool(safe_secret("SMTP_USE_SSL", False))
    use_tls = _as_bool(safe_secret("SMTP_USE_TLS", not use_ssl), default=not use_ssl)
    if not host or not from_email:
        return False, "Correo no configurado: faltan SMTP_HOST o SMTP_FROM_EMAIL."

    message = EmailMessage()
    message["Subject"] = " ".join((subject or "Nueva solicitud").splitlines())
    message["From"] = from_email
    message["To"] = ", ".join(recipients)
    message.set_content(body or "Nueva solicitud registrada.")

    try:
        context = ssl.create_default_context()
        client_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        with client_class(host, port, timeout=15) as client:
            if use_tls and not use_ssl:
                client.starttls(context=context)
            if username:
                client.login(username, password)
            client.send_message(message)
        return True, f"Correo enviado a {len(recipients)} destinatario(s)."
    except Exception as exc:
        logger.error(f"No se pudo enviar correo SMTP: {type(exc).__name__}: {exc}")
        return False, "La solicitud se guardó, pero el correo no pudo enviarse. Revisa la configuración SMTP."


def _get_whatsapp_client():
    values = {key: safe_secret(key, "") for key in WHATSAPP_SECRETS}
    if not all(values.values()):
        return None
    try:
        from twilio.rest import Client
        return Client(values["TWILIO_ACCOUNT_SID"], values["TWILIO_AUTH_TOKEN"])
    except Exception as exc:
        logger.warning(f"Twilio no disponible: {exc}")
        return None


def send_whatsapp_alert(message: str) -> bool:
    client = _get_whatsapp_client()
    if not client:
        return False
    try:
        from_number = safe_secret("TWILIO_WHATSAPP_FROM_NUMBER")
        to_number = safe_secret("DESTINATION_WHATSAPP_NUMBER")
        client.messages.create(
            from_=f"whatsapp:{from_number}", body=message, to=f"whatsapp:{to_number}"
        )
        return True
    except Exception as exc:
        logger.error(f"Error al enviar alerta de WhatsApp: {exc}")
        return False


def send_whatsapp_alert_async(message: str) -> None:
    """Envia la alerta en segundo plano: la respuesta de Twilio no debe retrasar
    la interfaz y su resultado nunca altera la operacion que la origino."""
    threading.Thread(target=send_whatsapp_alert, args=(message,), daemon=True).start()
