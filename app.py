import io
import json
import logging
import os
import re
import sys
import threading
import uuid
from datetime import datetime

import stripe
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

from analytics import log_scan_to_bigquery
from cloud_storage import download_report_from_gcs, upload_report_to_gcs

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
except ImportError:
    Limiter = None


try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


app = Flask(__name__)
CORS(app)

if Limiter:
    limiter = Limiter(
        get_remote_address,
        app=app,
        default_limits=[os.getenv("PRAETOR_DEFAULT_RATE_LIMIT", "300 per day")],
        storage_uri=os.getenv("RATELIMIT_STORAGE_URI", "memory://"),
    )
else:
    limiter = None


def rate_limit(rule):
    def decorator(func):
        if limiter:
            return limiter.limit(rule)(func)
        return func

    return decorator


def _json_error(message, status_code):
    return jsonify({"error": message}), status_code


def _is_paid_lead(lead):
    return bool(lead) and lead.get("status") == "paid"


TRUE_ENV_VALUES = {"1", "true", "yes", "on"}


def _env_enabled(name):
    return os.getenv(name, "").strip().lower() in TRUE_ENV_VALUES


def _env_configured(name, placeholders=None):
    placeholders = set(placeholders or [])
    value = os.getenv(name, "").strip()
    return bool(value) and value not in placeholders


BASE_URL = os.getenv("BASE_URL", "http://localhost:5000")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "sk_test_placeholder")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "whsec_placeholder")
DATABASE_URL = os.getenv("DATABASE_URL", "")

stripe.api_key = STRIPE_SECRET_KEY

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PDF_DIR = os.path.join(BASE_DIR, "pdf_reports")
LOG_DIR = os.path.join(BASE_DIR, "logs")
TEMPLATE_FILE = os.path.join(BASE_DIR, "TEMPLATES.html")
LANDING_FILE = os.path.join(BASE_DIR, "landing.html")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
ALLOWED_PLANS = {"express", "pro", "corporate"}

for folder in (PDF_DIR, LOG_DIR):
    os.makedirs(folder, exist_ok=True)


try:
    from logger_config import (
        download_logger,
        email_logger,
        payment_logger,
        scan_logger,
        webhook_logger,
    )
except ImportError:
    payment_logger = logging.getLogger("payment")
    email_logger = logging.getLogger("email")
    download_logger = logging.getLogger("download")
    webhook_logger = logging.getLogger("webhook")
    scan_logger = logging.getLogger("scan")
    logging.basicConfig(level=logging.INFO)


_log_handlers = [logging.StreamHandler()]
try:
    _log_handlers.append(logging.FileHandler(os.path.join(LOG_DIR, "app.log")))
except Exception:
    pass

logging.root.setLevel(logging.INFO)
if not logging.root.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=_log_handlers,
    )

logger = logging.getLogger(__name__)


try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from psycopg2.pool import SimpleConnectionPool

    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False
    logger.warning("psycopg2 not available; falling back to leads.json")


_db_pool = None
_db_pool_lock = threading.Lock()
_file_lock = threading.Lock()
_report_jobs = set()
_report_jobs_lock = threading.Lock()


def _use_db():
    return bool(DATABASE_URL) and PSYCOPG2_AVAILABLE


def _get_conn():
    global _db_pool

    if not _db_pool:
        with _db_pool_lock:
            if not _db_pool:
                maxconn = int(os.getenv("DB_POOL_MAXCONN", "5"))
                _db_pool = SimpleConnectionPool(
                    minconn=1,
                    maxconn=maxconn,
                    dsn=DATABASE_URL,
                    sslmode="require",
                )

    return _db_pool.getconn()


def _put_conn(conn):
    if _db_pool and conn:
        _db_pool.putconn(conn)
    elif conn:
        conn.close()


def _serialize_dates(data):
    for key in ("created_at", "paid_date"):
        if data.get(key) and hasattr(data[key], "isoformat"):
            data[key] = data[key].isoformat()


def init_db():
    if not _use_db():
        return

    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS leads (
                id SERIAL PRIMARY KEY,
                email TEXT,
                domain TEXT NOT NULL,
                report_id TEXT UNIQUE NOT NULL,
                plan TEXT DEFAULT 'express',
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW(),
                paid_date TIMESTAMP,
                stripe_session TEXT,
                pdf_file TEXT,
                gcs_uri TEXT,
                pdf_data BYTEA
            );
            """
        )
        cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS gcs_uri TEXT")
        conn.commit()
        cur.close()
        logger.info("PostgreSQL leads table ready")
    except Exception as exc:
        logger.error("init_db error: %s", exc)
    finally:
        _put_conn(conn)


def db_get_all_leads():
    if not _use_db():
        return _file_load_leads()

    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT id, email, domain, report_id, plan, status, created_at,
                   paid_date, stripe_session, pdf_file, gcs_uri
            FROM leads
            ORDER BY created_at DESC
            """
        )
        rows = [dict(row) for row in cur.fetchall()]
        cur.close()
    finally:
        _put_conn(conn)

    for row in rows:
        _serialize_dates(row)
    return rows


def db_get_lead_by_email(email):
    if not _use_db():
        leads = _file_load_leads()
        for index, lead in enumerate(leads):
            if lead.get("email") == email:
                return index, lead
        return None, None

    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT * FROM leads WHERE email=%s ORDER BY created_at DESC LIMIT 1",
            (email,),
        )
        row = cur.fetchone()
        cur.close()
    finally:
        _put_conn(conn)

    if not row:
        return None, None

    lead = dict(row)
    lead.pop("pdf_data", None)
    _serialize_dates(lead)
    return 0, lead


def db_get_lead_by_report_id(report_id):
    if not _use_db():
        leads = _file_load_leads()
        for index, lead in enumerate(leads):
            if (
                lead.get("report_id") == report_id
                or lead.get("pdf_file") == f"REPORT_{report_id}.pdf"
            ):
                return index, lead
        return None, None

    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT * FROM leads WHERE report_id=%s OR pdf_file=%s LIMIT 1",
            (report_id, f"REPORT_{report_id}.pdf"),
        )
        row = cur.fetchone()
        cur.close()
    finally:
        _put_conn(conn)

    if not row:
        return None, None

    lead = dict(row)
    lead.pop("pdf_data", None)
    _serialize_dates(lead)
    return 0, lead


def db_upsert_lead(lead):
    if not _use_db():
        return _file_upsert_lead(lead)

    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO leads (email, domain, report_id, plan, status, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (report_id) DO UPDATE SET
                email = EXCLUDED.email,
                domain = EXCLUDED.domain,
                plan = EXCLUDED.plan,
                status = EXCLUDED.status
            """,
            (
                lead.get("email"),
                lead.get("domain"),
                lead.get("report_id"),
                lead.get("plan", "express"),
                lead.get("status", "pending"),
                lead.get("created_at", datetime.now().isoformat()),
            ),
        )
        conn.commit()
        cur.close()
    finally:
        _put_conn(conn)


def db_update_lead(report_id, **kwargs):
    if not _use_db():
        return _file_update_lead(report_id, **kwargs)
    if not kwargs:
        return None

    pdf_data = kwargs.pop("pdf_data", None)
    field_map = {
        "status": "status",
        "paid_date": "paid_date",
        "stripe_session": "stripe_session",
        "pdf_file": "pdf_file",
        "gcs_uri": "gcs_uri",
        "domain": "domain",
        "plan": "plan",
    }
    set_clauses = []
    values = []
    for key, value in kwargs.items():
        column = field_map.get(key)
        if column:
            set_clauses.append(f"{column} = %s")
            values.append(value)

    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        if set_clauses:
            values.append(report_id)
            cur.execute(
                f"UPDATE leads SET {', '.join(set_clauses)} WHERE report_id=%s",
                values,
            )
        if pdf_data is not None:
            cur.execute(
                "UPDATE leads SET pdf_data=%s WHERE report_id=%s",
                (psycopg2.Binary(pdf_data), report_id),
            )
        conn.commit()
        cur.close()
    finally:
        _put_conn(conn)

    return None


def db_get_pdf_bytes(report_id):
    if not _use_db():
        return None

    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("SELECT pdf_data FROM leads WHERE report_id=%s", (report_id,))
        row = cur.fetchone()
        cur.close()
    finally:
        _put_conn(conn)

    if row and row[0]:
        return bytes(row[0])
    return None


def _file_load_leads():
    path = os.path.join(BASE_DIR, "leads.json")
    with _file_lock:
        try:
            with open(path, "r", encoding="utf-8") as file:
                return json.load(file)
        except Exception:
            return []


def _file_save_leads(leads):
    with _file_lock:
        _file_save_leads_unlocked(leads)


def _file_save_leads_unlocked(leads):
    path = os.path.join(BASE_DIR, "leads.json")
    tmp_path = f"{path}.tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump(leads, file, indent=2)
    os.replace(tmp_path, path)


def _file_upsert_lead(lead):
    with _file_lock:
        leads = _file_load_leads_unlocked()
        for current in leads:
            if current.get("report_id") == lead.get("report_id"):
                current.update(lead)
                break
        else:
            leads.append(lead)
        _file_save_leads_unlocked(leads)


def _file_update_lead(report_id, **kwargs):
    with _file_lock:
        leads = _file_load_leads_unlocked()
        for lead in leads:
            if lead.get("report_id") == report_id:
                for key, value in kwargs.items():
                    if key != "pdf_data":
                        lead[key] = value
                break
        _file_save_leads_unlocked(leads)


def _file_load_leads_unlocked():
    path = os.path.join(BASE_DIR, "leads.json")
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception:
        return []


def generate_pdf_with_id(domain, report_id=None, depth="express", customer_email=None):
    if not report_id:
        report_id = str(uuid.uuid4())[:8]

    pdf_filename = f"REPORT_{report_id}.pdf"
    pdf_path = os.path.join(PDF_DIR, pdf_filename)
    scan_result = None

    try:
        from report_generator import generate_report
        from scan_target import scan_target

        scan_logger.info("Scanning %s (depth=%s, report=%s)", domain, depth, report_id)
        scan_result = scan_target(domain, depth=depth)
        generate_report(scan_result, output_path=pdf_path)
        scan_logger.info("PDF generated: %s", pdf_filename)
    except Exception as exc:
        logger.error("PDF generation failed: %s", exc)
        scan_logger.error("PDF generation failed: %s", exc)
        try:
            from reportlab.pdfgen import canvas

            doc = canvas.Canvas(pdf_path)
            doc.drawString(72, 720, f"PRAETOR Intelligence - {domain}")
            doc.drawString(72, 700, f"Report ID: {report_id}")
            doc.drawString(72, 680, f"Generated: {datetime.now().isoformat()}")
            doc.drawString(72, 640, f"Scan error: {str(exc)[:200]}")
            doc.save()
        except Exception:
            with open(pdf_path, "wb") as file:
                file.write(b"%PDF-1.4\n")

    try:
        with open(pdf_path, "rb") as file:
            pdf_bytes = file.read()
        gcs_uri = upload_report_to_gcs(pdf_path, pdf_filename)
        update_fields = {"pdf_file": pdf_filename, "pdf_data": pdf_bytes}
        if gcs_uri:
            update_fields["gcs_uri"] = gcs_uri
        db_update_lead(report_id, **update_fields)
        analytics_result = log_scan_to_bigquery(
            report_id=report_id,
            domain=domain,
            scan_result=scan_result,
            customer_email=customer_email,
            gcs_uri=gcs_uri,
        )
        if analytics_result.get("status") not in ("disabled", "skipped"):
            logger.info("BigQuery analytics result: %s", analytics_result)
        logger.info("PDF stored: %s (%s bytes)", pdf_filename, len(pdf_bytes))
    except Exception as exc:
        logger.error("Failed to store PDF: %s", exc)

    return {
        "report_id": report_id,
        "pdf_path": pdf_path,
        "pdf_filename": pdf_filename,
    }


def queue_report_generation(domain, report_id, depth, email=None):
    with _report_jobs_lock:
        if report_id in _report_jobs:
            return False
        _report_jobs.add(report_id)

    def _background_work():
        try:
            result = generate_pdf_with_id(
                domain,
                report_id,
                depth=depth,
                customer_email=email,
            )
            pdf_path = result["pdf_path"]
            webhook_logger.info("PDF ready: %s", result["pdf_filename"])
            if email:
                send_report_email(email, pdf_path, domain, report_id)
        except Exception as exc:
            webhook_logger.error("Background report generation failed: %s", exc)
        finally:
            with _report_jobs_lock:
                _report_jobs.discard(report_id)

    threading.Thread(target=_background_work, daemon=True).start()
    return True


def send_report_email(email, pdf_path, domain, report_id):
    email_logger.info("Email requested")
    email_logger.info(" - recipient: %s", email)
    email_logger.info(" - attachment: %s", pdf_path)
    email_logger.info(" - domain: %s", domain)
    email_logger.info(" - report_id: %s", report_id)

    try:
        from email_sender import enviar_reporte_por_email

        result = enviar_reporte_por_email(email, domain, pdf_path)
        email_logger.info("Email completed: %s", email)
        return result
    except Exception as exc:
        email_logger.error("Email error: %s", exc)
        return False


def _email_health_status():
    try:
        from email_sender import email_config_status

        return email_config_status()
    except Exception as exc:
        return {
            "enabled": False,
            "provider": "unavailable",
            "configured": False,
            "error": type(exc).__name__,
        }


def _health_payload(deep=False):
    checks = {
        "database": {
            "mode": "postgres" if _use_db() else "file",
            "configured": bool(DATABASE_URL),
            "available": True,
        },
        "stripe_secret_key": {
            "configured": _env_configured("STRIPE_SECRET_KEY", {"sk_test_placeholder"}),
        },
        "stripe_webhook_secret": {
            "configured": _env_configured("STRIPE_WEBHOOK_SECRET", {"whsec_placeholder"}),
        },
        "admin_token": {"configured": _env_configured("ADMIN_TOKEN")},
        "base_url": {
            "configured": _env_configured("BASE_URL"),
            "is_local": BASE_URL.startswith("http://localhost")
            or BASE_URL.startswith("http://127.0.0.1"),
        },
        "gcs": {
            "configured": _env_configured("GCS_BUCKET_NAME")
            or _env_configured("PRAETOR_REPORTS_BUCKET"),
        },
        "bigquery": {
            "enabled": _env_enabled("PRAETOR_BIGQUERY_ENABLED"),
            "dataset": os.getenv("BQ_DATASET", "praetor_analytics"),
            "table": os.getenv("BQ_TABLE", "security_audits"),
        },
        "email": _email_health_status(),
        "rate_limit_storage": {
            "uri": os.getenv("RATELIMIT_STORAGE_URI", "memory://"),
            "production_safe": os.getenv("RATELIMIT_STORAGE_URI", "memory://") != "memory://",
        },
    }

    if deep and _use_db():
        conn = None
        try:
            conn = _get_conn()
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            cur.close()
        except Exception as exc:
            checks["database"]["available"] = False
            checks["database"]["error"] = type(exc).__name__
        finally:
            _put_conn(conn)

    production_ready = all(
        (
            checks["database"]["configured"],
            checks["database"]["available"],
            checks["stripe_secret_key"]["configured"],
            checks["stripe_webhook_secret"]["configured"],
            checks["admin_token"]["configured"],
            checks["base_url"]["configured"],
            not checks["base_url"]["is_local"],
            checks["rate_limit_storage"]["production_safe"],
        )
    )

    return {
        "status": "online",
        "version": "1.0.0",
        "db": checks["database"]["mode"],
        "mode": "production-ready" if production_ready else "demo",
        "production_ready": production_ready,
        "checks": checks,
    }


@app.route("/")
def home():
    if os.path.exists(LANDING_FILE):
        with open(LANDING_FILE, "r", encoding="utf-8") as file:
            return file.read(), 200, {"Content-Type": "text/html; charset=utf-8"}

    return jsonify(
        {
            "name": "PRAETOR Intelligence",
            "version": "1.0.0",
            "status": "online",
        }
    )


@app.route("/api/health")
@rate_limit("60 per minute")
def api_health():
    deep = request.args.get("deep", "").strip().lower() in TRUE_ENV_VALUES
    return jsonify(_health_payload(deep=deep))


@app.route("/ping")
@rate_limit("60 per minute")
def ping():
    return jsonify({"ok": True, "deploy": "pg-migration"})


@app.route("/scan", methods=["POST"])
@rate_limit(os.getenv("PRAETOR_SCAN_RATE_LIMIT", "12 per minute"))
def scan_preview():
    try:
        from scan_target import _is_valid_domain, scan_target
    except Exception as exc:
        return jsonify({"error": f"Scanner unavailable: {exc}"}), 500

    data = request.get_json(silent=True) or {}
    domain = (data.get("domain") or "").strip().lower()
    domain = re.sub(r"^https?://", "", domain).split("/")[0]

    if not _is_valid_domain(domain):
        return jsonify({"error": "Dominio no valido. Usa un formato como empresa.com"}), 400

    scan_logger.info("Free preview scan: %s", domain)
    try:
        result = scan_target(domain, depth="express")
    except Exception as exc:
        scan_logger.error("Preview scan failed for %s: %s", domain, exc)
        return jsonify({"error": f"No se pudo escanear {domain}."}), 502

    ssl_info = result.get("ssl") or {}
    findings = []
    if not result.get("spf"):
        findings.append("Sin registro SPF")
    if not result.get("dmarc"):
        findings.append("Sin politica DMARC")
    if ssl_info.get("error"):
        findings.append("Problema con el certificado SSL/TLS")

    return jsonify(
        {
            "domain": result.get("domain"),
            "ip": result.get("ip"),
            "risk_score": result.get("risk_score"),
            "risk_level": result.get("risk_level"),
            "spf_configured": bool(result.get("spf")),
            "dmarc_configured": bool(result.get("dmarc")),
            "ssl_ok": not bool(ssl_info.get("error")),
            "ssl_issuer": ssl_info.get("issuer"),
            "technologies": result.get("technologies") or [],
            "key_findings": findings,
            "scanned_at": result.get("scanned_at"),
        }
    )


@app.route("/status")
@rate_limit("30 per minute")
def status():
    leads = db_get_all_leads()
    paid_count = len([lead for lead in leads if lead.get("status") == "paid"])
    pdf_count = (
        len([name for name in os.listdir(PDF_DIR) if name.endswith(".pdf")])
        if os.path.exists(PDF_DIR)
        else 0
    )

    return jsonify(
        {
            "status": "online",
            "timestamp": datetime.now().isoformat(),
            "pdf_count": pdf_count,
            "lead_count": len(leads),
            "paid_count": paid_count,
            "storage": "postgres" if _use_db() else "file",
            "active_report_jobs": len(_report_jobs),
        }
    )


@app.route("/leads")
@rate_limit("30 per minute")
def list_leads():
    if not ADMIN_TOKEN:
        return _json_error("ADMIN_TOKEN is not configured.", 503)

    auth = request.headers.get("Authorization", "")
    token = request.args.get("token", "")
    if auth != f"Bearer {ADMIN_TOKEN}" and token != ADMIN_TOKEN:
        return _json_error("Unauthorized", 401)

    return jsonify(db_get_all_leads())


@app.route("/create-checkout-session", methods=["POST"])
@rate_limit(os.getenv("PRAETOR_CHECKOUT_RATE_LIMIT", "20 per minute"))
def create_checkout_session():
    try:
        from scan_target import _is_valid_domain, normalize_domain

        data = request.get_json(silent=True) or {}
        domain = normalize_domain(data.get("domain", "example.com"))
        if not _is_valid_domain(domain):
            return _json_error("Dominio no valido. Usa un formato como empresa.com", 400)

        email = (data.get("email") or "").strip()
        if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email):
            email = ""

        try:
            price_amount = int(data.get("price", 100))
        except (TypeError, ValueError):
            return _json_error("El precio debe ser un entero en centavos.", 400)
        if price_amount < 100:
            return _json_error("El precio minimo es 100 centavos.", 400)

        plan = data.get("plan", "express")
        if plan not in ALLOWED_PLANS:
            return _json_error("Plan no valido.", 400)

        report_id = str(uuid.uuid4())[:8]

        db_upsert_lead(
            {
                "email": email,
                "domain": domain,
                "report_id": report_id,
                "plan": plan,
                "status": "pending",
                "created_at": datetime.now().isoformat(),
            }
        )
        payment_logger.info("Lead saved: %s - %s - %s", email, domain, report_id)

        session_params = {
            "payment_method_types": ["card"],
            "line_items": [
                {
                    "price_data": {
                        "currency": os.getenv("STRIPE_CURRENCY", "mxn"),
                        "product_data": {
                            "name": f"PRAETOR Report - {domain}",
                            "description": f"Security analysis report for {domain}",
                        },
                        "unit_amount": price_amount,
                    },
                    "quantity": 1,
                }
            ],
            "mode": "payment",
            "customer_creation": "always",
            "success_url": f"{BASE_URL}/success/{report_id}?domain={domain}&plan={plan}",
            "cancel_url": BASE_URL,
            "metadata": {
                "report_id": report_id,
                "domain": domain,
                "email": email,
                "plan": plan,
            },
        }
        if email:
            session_params["customer_email"] = email
            session_params["payment_intent_data"] = {"receipt_email": email}

        session = stripe.checkout.Session.create(**session_params)
        return jsonify(
            {"session_id": session.id, "url": session.url, "report_id": report_id}
        )
    except Exception as exc:
        logger.error("Error creating Stripe session: %s", exc)
        return jsonify({"error": str(exc)}), 400


@app.route("/webhook/stripe", methods=["POST"])
@rate_limit(os.getenv("PRAETOR_WEBHOOK_RATE_LIMIT", "120 per minute"))
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError as exc:
        webhook_logger.error("Invalid payload: %s", exc)
        return jsonify({"error": "Invalid payload"}), 400
    except stripe.error.SignatureVerificationError as exc:
        webhook_logger.error("Invalid signature: %s", exc)
        return jsonify({"error": "Invalid signature"}), 400

    if event["type"] != "checkout.session.completed":
        return jsonify({"status": "ok"}), 200

    session = event["data"]["object"]
    session_data = dict(session)
    customer_details = session_data.get("customer_details") or {}
    if not isinstance(customer_details, dict):
        customer_details = dict(customer_details)
    metadata = session_data.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = dict(metadata)

    customer_email = customer_details.get("email")
    report_id = metadata.get("report_id")
    domain = metadata.get("domain")
    plan = metadata.get("plan", "express")
    session_id = session_data.get("id")
    if not report_id:
        webhook_logger.error("Missing report_id in Stripe webhook metadata")
        return _json_error("Missing report_id", 400)
    if plan not in ALLOWED_PLANS:
        plan = "express"

    _, lead = db_get_lead_by_report_id(report_id)
    if not lead:
        _, lead = db_get_lead_by_email(customer_email)

    effective_domain = domain or (lead or {}).get("domain", "unknown")
    effective_plan = (lead or {}).get("plan", plan)

    db_upsert_lead(
        {
            "email": customer_email,
            "domain": effective_domain,
            "report_id": report_id,
            "plan": effective_plan,
            "status": "pending",
            "created_at": datetime.now().isoformat(),
        }
    )
    db_update_lead(
        report_id,
        status="paid",
        paid_date=datetime.now().isoformat(),
        stripe_session=session_id,
        domain=effective_domain,
    )

    queued = queue_report_generation(
        effective_domain,
        report_id,
        depth=effective_plan,
        email=customer_email,
    )
    return jsonify({"status": "queued" if queued else "already_queued", "report_id": report_id}), 200


@app.route("/download/<report_id>")
@rate_limit(os.getenv("PRAETOR_DOWNLOAD_RATE_LIMIT", "60 per minute"))
def download_report(report_id):
    download_logger.info("Download requested: %s", report_id)
    pdf_filename = f"REPORT_{report_id}.pdf"
    _, lead = db_get_lead_by_report_id(report_id)
    if not _is_paid_lead(lead):
        return _json_error("Report not found or payment not confirmed.", 404)

    pdf_bytes = db_get_pdf_bytes(report_id)
    if pdf_bytes:
        return send_file(
            io.BytesIO(pdf_bytes),
            as_attachment=True,
            download_name=pdf_filename,
            mimetype="application/pdf",
        )

    pdf_path = os.path.join(PDF_DIR, pdf_filename)
    if not os.path.exists(pdf_path):
        if lead and lead.get("pdf_file"):
            pdf_path = os.path.join(PDF_DIR, lead["pdf_file"])
            pdf_filename = lead["pdf_file"]

    if os.path.exists(pdf_path):
        try:
            with open(pdf_path, "rb") as file:
                pdf_bytes = file.read()
            db_update_lead(report_id, pdf_data=pdf_bytes, pdf_file=pdf_filename)
        except Exception:
            pass

        return send_file(
            pdf_path,
            as_attachment=True,
            download_name=pdf_filename,
            mimetype="application/pdf",
        )

    gcs_uri = (lead or {}).get("gcs_uri")
    pdf_bytes = download_report_from_gcs(pdf_filename, gcs_uri=gcs_uri)
    if pdf_bytes:
        try:
            db_update_lead(report_id, pdf_data=pdf_bytes, pdf_file=pdf_filename)
        except Exception:
            pass
        return send_file(
            io.BytesIO(pdf_bytes),
            as_attachment=True,
            download_name=pdf_filename,
            mimetype="application/pdf",
        )

    if _is_paid_lead(lead):
        domain = lead.get("domain", "unknown")
        plan = lead.get("plan", "express")
        queued = queue_report_generation(domain, report_id, depth=plan)
        return (
            jsonify(
                {
                    "error": "Report is still generating. Try again in a moment.",
                    "queued": queued,
                }
            ),
            202,
        )

    return jsonify({"error": "Report not found."}), 404


@app.route("/success/<report_id>")
@rate_limit("30 per minute")
def success_page(report_id):
    _, lead = db_get_lead_by_report_id(report_id)
    if not lead:
        return _json_error("Report not found.", 404)
    if not _is_paid_lead(lead):
        return (
            jsonify(
                {
                    "status": "pending",
                    "message": "Payment confirmation is still pending. Try again in a moment.",
                }
            ),
            202,
        )

    pdf_file = lead.get("pdf_file") if lead else None

    if not pdf_file:
        domain = lead.get("domain", "unknown")
        plan = lead.get("plan", "express")
        email = lead.get("email")
        queue_report_generation(domain, report_id, depth=plan, email=email)

    if os.path.exists(TEMPLATE_FILE):
        with open(TEMPLATE_FILE, "r", encoding="utf-8") as file:
            html_content = file.read()
        html_content = html_content.replace("{{ report_id }}", report_id)
        html_content = html_content.replace("{{ pdf_file }}", pdf_file or "")
        return html_content, 200, {"Content-Type": "text/html; charset=utf-8"}

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>PRAETOR - Descarga tu Reporte</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                background: #0a0a0a;
                color: #fff;
                display: flex;
                justify-content: center;
                align-items: center;
                height: 100vh;
                margin: 0;
            }}
            .box {{
                text-align: center;
                background: #1a1a1a;
                padding: 40px;
                border-radius: 12px;
                border: 1px solid #00d4ff;
                max-width: 500px;
            }}
            h1 {{ color: #00d4ff; }}
            .btn {{
                background: #00d4ff;
                color: #000;
                border: none;
                padding: 14px 35px;
                font-size: 18px;
                font-weight: bold;
                border-radius: 8px;
                cursor: pointer;
                text-decoration: none;
                display: inline-block;
            }}
            .info {{
                color: #666;
                font-size: 14px;
                margin-top: 15px;
            }}
        </style>
    </head>
    <body>
        <div class="box">
            <h1>Pago confirmado</h1>
            <p style="color:#888;">Tu reporte esta listo o se esta generando.</p>
            <a href="/download/{report_id}" class="btn">Descargar reporte</a>
            <p class="info">Guarda el PDF en tu dispositivo.</p>
        </div>
        <script>
            setTimeout(() => {{
                window.location.href = "/download/{report_id}";
            }}, 1500);
        </script>
    </body>
    </html>
    """


@app.route("/test-payment", methods=["GET"])
@rate_limit("10 per minute")
def test_payment():
    domain = request.args.get("domain", "example.com")
    email = request.args.get("email", "test@praetor.lat")
    report_id = "TEST_" + str(uuid.uuid4())[:6]
    results = {}

    try:
        db_upsert_lead(
            {
                "email": email,
                "domain": domain,
                "report_id": report_id,
                "plan": "express",
                "status": "pending",
                "created_at": datetime.now().isoformat(),
            }
        )
        results["1_lead"] = f"PASS (storage={'postgres' if _use_db() else 'file'})"
    except Exception as exc:
        results["1_lead"] = f"FAIL: {exc}"

    pdf_filename = f"REPORT_{report_id}.pdf"
    pdf_path = os.path.join(PDF_DIR, pdf_filename)
    try:
        from reportlab.pdfgen import canvas

        doc = canvas.Canvas(pdf_path)
        doc.setFont("Helvetica-Bold", 16)
        doc.drawString(72, 750, "PRAETOR Intelligence - Test Report")
        doc.setFont("Helvetica", 12)
        doc.drawString(72, 720, f"Domain : {domain}")
        doc.drawString(72, 700, f"Report : {report_id}")
        doc.drawString(72, 680, f"Date : {datetime.now().isoformat()}")
        doc.drawString(72, 640, "Reporte de prueba local sin scan real.")
        doc.save()
        results["2_pdf_generated"] = f"PASS ({os.path.getsize(pdf_path)} bytes)"
    except Exception as exc:
        results["2_pdf_generated"] = f"FAIL: {exc}"

    try:
        if os.path.exists(pdf_path):
            with open(pdf_path, "rb") as file:
                pdf_bytes = file.read()
            db_update_lead(
                report_id,
                status="paid",
                paid_date=datetime.now().isoformat(),
                pdf_file=pdf_filename,
                pdf_data=pdf_bytes,
            )
            results["3_pdf_registered"] = f"PASS ({len(pdf_bytes)} bytes)"
        else:
            results["3_pdf_registered"] = "FAIL: PDF file was not generated"
    except Exception as exc:
        results["3_pdf_registered"] = f"FAIL: {exc}"

    try:
        retrieved = db_get_pdf_bytes(report_id)
        if retrieved and len(retrieved) > 100:
            results["4_download_source"] = "PASS (database)"
        elif os.path.exists(pdf_path):
            results["4_download_source"] = "PASS (filesystem)"
        else:
            results["4_download_source"] = "FAIL: PDF not found"
    except Exception as exc:
        results["4_download_source"] = f"FAIL: {exc}"

    if request.args.get("email_test") == "1":
        try:
            from email_sender import enviar_reporte_por_email

            ok = enviar_reporte_por_email(email, domain, pdf_path)
            results["5_email"] = "PASS" if ok else "FAIL: returned False"
        except Exception as exc:
            results["5_email"] = f"FAIL: {type(exc).__name__}: {exc}"
    else:
        results["5_email"] = "SKIP (use &email_test=1 to test email)"

    passed = sum(1 for value in results.values() if str(value).startswith("PASS"))
    results["_summary"] = f"{passed} steps PASS"
    results["_report_id"] = report_id
    results["_download_url"] = f"/download/{report_id}"

    return jsonify(results), 200


@app.route("/cron/monitor", methods=["POST", "GET"])
@rate_limit(os.getenv("PRAETOR_CRON_RATE_LIMIT", "20 per minute"))
def cron_monitor():
    secret = os.getenv("CRON_SECRET", "")
    if secret:
        incoming = request.headers.get("X-Cron-Secret") or request.args.get("secret", "")
        if incoming != secret:
            return jsonify({"error": "Unauthorized"}), 403

    try:
        from monitor import run_monitor
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    leads = db_get_all_leads()
    results = []
    for lead in leads:
        if lead.get("status") != "paid":
            continue
        plan = lead.get("plan", "express")
        if plan not in ("pro", "corporate"):
            continue
        domain = lead.get("domain")
        if not domain:
            continue

        try:
            result = run_monitor(domain, depth=plan)
            diff = result.get("diff") or {}
            results.append(
                {
                    "domain": domain,
                    "is_first_scan": result["is_first_scan"],
                    "summary": diff.get("summary"),
                    "score_change": diff.get("score_change"),
                }
            )
        except Exception as exc:
            logger.error("[cron] %s failed: %s", domain, exc)
            results.append({"domain": domain, "error": str(exc)})

    return jsonify({"status": "done", "scanned": len(results), "results": results})


@app.errorhandler(404)
def not_found(_error):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def server_error(error):
    import traceback

    trace = traceback.format_exc()
    logger.error("Internal error: %s\n%s", error, trace)
    try:
        webhook_logger.error("Internal error:\n%s", trace)
    except Exception:
        pass
    return jsonify({"error": "Internal server error"}), 500


init_db()


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print("")
    print("=" * 50)
    print("PRAETOR Intelligence")
    print(f"http://localhost:{port}")
    print(f"Storage: {'PostgreSQL' if _use_db() else 'File (leads.json)'}")
    print(f"http://localhost:{port}/success/TEST123")
    print(f"http://localhost:{port}/status")
    print(f"http://localhost:{port}/leads")
    print("=" * 50)
    print("")
    logger.info("PRAETOR Intelligence started on http://localhost:%s", port)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
