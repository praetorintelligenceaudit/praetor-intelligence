import logging
import os
from datetime import datetime, timezone


logger = logging.getLogger(__name__)


TRUE_VALUES = {"1", "true", "yes", "on"}


def _enabled():
    return os.getenv("PRAETOR_BIGQUERY_ENABLED", "").strip().lower() in TRUE_VALUES


def _table_id():
    project_id = (
        os.getenv("BQ_PROJECT_ID")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
        or os.getenv("GCP_PROJECT")
        or "praetor-497703"
    )
    dataset = os.getenv("BQ_DATASET", "praetor_analytics")
    table = os.getenv("BQ_TABLE", "security_audits")
    return f"{project_id}.{dataset}.{table}"


def log_scan_to_bigquery(report_id, domain, scan_result, customer_email=None, gcs_uri=None):
    if not _enabled():
        return {
            "status": "disabled",
            "reason": "Set PRAETOR_BIGQUERY_ENABLED=1 to write audit rows.",
        }
    if not scan_result:
        return {"status": "skipped", "reason": "No scan_result available."}

    try:
        from google.cloud import bigquery
    except ImportError as exc:
        logger.warning("BigQuery client is not installed: %s", exc)
        return {"status": "skipped", "reason": "google-cloud-bigquery not installed"}

    row = {
        "report_id": report_id,
        "domain": domain,
        "risk_level": scan_result.get("risk_level"),
        "risk_score": scan_result.get("risk_score"),
        "findings": scan_result.get("findings") or [],
        "scan_payload": scan_result,
        "scan_date": datetime.now(timezone.utc).isoformat(),
        "customer_email": customer_email,
        "pdf_gcs_uri": gcs_uri,
        "scan_mode": scan_result.get("scan_mode"),
    }

    table_id = _table_id()
    client = bigquery.Client(project=table_id.split(".", 1)[0])
    errors = client.insert_rows_json(table_id, [row], ignore_unknown_values=True)
    if errors:
        logger.error("BigQuery insert failed: %s", errors)
        return {"status": "error", "table": table_id, "errors": errors}

    return {"status": "ok", "table": table_id}
