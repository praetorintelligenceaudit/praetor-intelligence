from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas


def _draw_wrapped_text(pdf, text, x, y, max_chars=92, line_height=14):
    words = str(text).split()
    line = ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if len(candidate) > max_chars:
            pdf.drawString(x, y, line)
            y -= line_height
            line = word
        else:
            line = candidate
    if line:
        pdf.drawString(x, y, line)
        y -= line_height
    return y


def generate_report(scan_result, output_path):
    pdf = canvas.Canvas(output_path, pagesize=letter)
    width, height = letter
    left = 0.75 * inch
    y = height - 0.75 * inch

    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawString(left, y, "PRAETOR Intelligence Security Report")
    y -= 28

    pdf.setFont("Helvetica", 11)
    for label, key in (
        ("Domain", "domain"),
        ("Scan mode", "scan_mode"),
        ("Scanned at", "scanned_at"),
        ("Risk score", "risk_score"),
        ("Risk level", "risk_level"),
        ("Primary IP", "ip"),
    ):
        pdf.drawString(left, y, f"{label}: {scan_result.get(key)}")
        y -= 16

    y -= 8
    pdf.setFont("Helvetica-Bold", 13)
    pdf.drawString(left, y, "Key findings")
    y -= 18
    pdf.setFont("Helvetica", 10)
    findings = scan_result.get("findings") or ["No major passive findings detected."]
    for finding in findings:
        y = _draw_wrapped_text(pdf, f"- {finding}", left, y)

    y -= 8
    pdf.setFont("Helvetica-Bold", 13)
    pdf.drawString(left, y, "Email authentication")
    y -= 18
    pdf.setFont("Helvetica", 10)
    y = _draw_wrapped_text(pdf, f"SPF: {scan_result.get('spf') or 'Not detected'}", left, y)
    y = _draw_wrapped_text(pdf, f"DMARC: {scan_result.get('dmarc') or 'Not detected'}", left, y)

    y -= 8
    pdf.setFont("Helvetica-Bold", 13)
    pdf.drawString(left, y, "TLS and HTTP")
    y -= 18
    pdf.setFont("Helvetica", 10)
    ssl_info = scan_result.get("ssl") or {}
    http_info = scan_result.get("http") or {}
    y = _draw_wrapped_text(pdf, f"TLS issuer: {ssl_info.get('issuer') or ssl_info.get('error')}", left, y)
    y = _draw_wrapped_text(pdf, f"TLS valid until: {ssl_info.get('not_after')}", left, y)
    y = _draw_wrapped_text(pdf, f"HTTP status: {http_info.get('status_code')}", left, y)
    y = _draw_wrapped_text(pdf, f"Final URL: {http_info.get('final_url')}", left, y)

    y -= 8
    pdf.setFont("Helvetica-Bold", 13)
    pdf.drawString(left, y, "Security headers")
    y -= 18
    pdf.setFont("Helvetica", 10)
    for header, value in (http_info.get("headers") or {}).items():
        y = _draw_wrapped_text(pdf, f"{header}: {value or 'Missing'}", left, y)

    pdf.setFont("Helvetica-Oblique", 8)
    pdf.drawString(
        left,
        0.5 * inch,
        "Passive low-intensity report. It does not include intrusive vulnerability testing.",
    )
    pdf.save()
    return output_path
