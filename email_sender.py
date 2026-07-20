import base64
import logging
import mimetypes
import os
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path

import requests


logger = logging.getLogger(__name__)

TRUE_VALUES = {"1", "true", "yes", "on"}
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _enabled():
    return os.getenv("PRAETOR_EMAIL_ENABLED", "").strip().lower() in TRUE_VALUES


def _configured(value):
    return bool((value or "").strip())


def email_config_status():
    sendgrid_key = os.getenv("SENDGRID_API_KEY", "")
    smtp_host = os.getenv("SMTP_HOST", "")
    smtp_user = os.getenv("SMTP_USERNAME", "")
    from_email = os.getenv("EMAIL_FROM") or os.getenv("SENDGRID_FROM_EMAIL", "")

    provider = "none"
    provider_configured = False
    if _configured(sendgrid_key):
        provider = "sendgrid"
        provider_configured = _configured(from_email)
    elif _configured(smtp_host):
        provider = "smtp"
        provider_configured = _configured(from_email) and _configured(smtp_user)

    return {
        "enabled": _enabled(),
        "provider": provider,
        "configured": _enabled() and provider_configured,
        "from_email_configured": _configured(from_email),
    }


def _validate_inputs(recipient_email, pdf_path):
    if not EMAIL_RE.match(recipient_email or ""):
        raise ValueError("recipient email is invalid")

    path = Path(pdf_path)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"PDF file not found: {path}")

    return path


def _message_parts(domain, report_id=None):
    subject = os.getenv("PRAETOR_EMAIL_SUBJECT") or f"PRAETOR Intelligence report - {domain}"
    download_hint = f"Report ID: {report_id}" if report_id else "Attached report."
    text = os.getenv("PRAETOR_EMAIL_TEXT") or (
        "Hola,\n\n"
        f"Adjunto el reporte pasivo de PRAETOR Intelligence para {domain}.\n"
        f"{download_hint}\n\n"
        "Este reporte es una revision externa pasiva; no incluye pruebas intrusivas.\n"
    )
    return subject, text


def _send_with_sendgrid(recipient_email, domain, pdf_path, report_id=None):
    api_key = os.getenv("SENDGRID_API_KEY", "").strip()
    from_email = (
        os.getenv("SENDGRID_FROM_EMAIL")
        or os.getenv("EMAIL_FROM")
        or ""
    ).strip()
    if not api_key or not from_email:
        raise RuntimeError("SendGrid requires SENDGRID_API_KEY and EMAIL_FROM/SENDGRID_FROM_EMAIL")

    subject, text = _message_parts(domain, report_id=report_id)
    content_type = mimetypes.guess_type(str(pdf_path))[0] or "application/pdf"
    encoded = base64.b64encode(pdf_path.read_bytes()).decode("ascii")

    payload = {
        "personalizations": [{"to": [{"email": recipient_email}]}],
        "from": {"email": from_email},
        "subject": subject,
        "content": [{"type": "text/plain", "value": text}],
        "attachments": [
            {
                "content": encoded,
                "type": content_type,
                "filename": pdf_path.name,
                "disposition": "attachment",
            }
        ],
    }

    response = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=float(os.getenv("PRAETOR_EMAIL_TIMEOUT", "15")),
    )
    if response.status_code not in (200, 202):
        raise RuntimeError(f"SendGrid rejected email with HTTP {response.status_code}")
    return True


def _send_with_smtp(recipient_email, domain, pdf_path, report_id=None):
    host = os.getenv("SMTP_HOST", "").strip()
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    from_email = os.getenv("EMAIL_FROM", "").strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    use_tls = os.getenv("SMTP_USE_TLS", "1").strip().lower() in TRUE_VALUES

    if not host or not username or not password or not from_email:
        raise RuntimeError("SMTP requires SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD and EMAIL_FROM")

    subject, text = _message_parts(domain, report_id=report_id)
    message = EmailMessage()
    message["From"] = from_email
    message["To"] = recipient_email
    message["Subject"] = subject
    message.set_content(text)
    message.add_attachment(
        pdf_path.read_bytes(),
        maintype="application",
        subtype="pdf",
        filename=pdf_path.name,
    )

    with smtplib.SMTP(host, port, timeout=float(os.getenv("PRAETOR_EMAIL_TIMEOUT", "15"))) as smtp:
        if use_tls:
            smtp.starttls()
        smtp.login(username, password)
        smtp.send_message(message)

    return True


def enviar_reporte_por_email(recipient_email, domain, pdf_path, report_id=None):
    if not _enabled():
        logger.warning("Email delivery disabled. Set PRAETOR_EMAIL_ENABLED=1 to enable.")
        return False

    path = _validate_inputs(recipient_email, pdf_path)

    if os.getenv("SENDGRID_API_KEY", "").strip():
        return _send_with_sendgrid(recipient_email, domain, path, report_id=report_id)

    if os.getenv("SMTP_HOST", "").strip():
        return _send_with_smtp(recipient_email, domain, path, report_id=report_id)

    raise RuntimeError("No email provider configured. Use SendGrid or SMTP environment variables.")
