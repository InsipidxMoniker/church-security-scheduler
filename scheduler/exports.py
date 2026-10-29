"""Fixed-layout exports consume only an explicitly selected immutable payload."""
import io
from pathlib import Path
import threading
import unicodedata
from xml.sax.saxutils import escape

FONT_LOCK = threading.Lock()
PDF_GATE = threading.BoundedSemaphore(2)


def bounded_pdf(payload, current=True):
    from scheduler.security import Problem
    if not PDF_GATE.acquire(blocking=False):
        raise Problem(429, "PDF export busy; retry shortly", 2)
    try:
        return pdf_bytes(payload, current)
    finally:
        PDF_GATE.release()


def slot_text(slot):
    if slot["volunteer_id"]:
        text = slot["display_name"]
        if slot["rotation_approved"]:
            text += " [approved same-zone exception]"
        return text
    return "VACANT [approved essential vacancy]" if slot["essential"] else "VACANT [optional]"


def position_text(payload, slot):
    # Older immutable payloads had no zone display labels; retain their exact
    # output representation instead of consulting live settings or inventing one.
    label = payload["config"].get(slot["zone"] + "_zone_label")
    return str(slot["position_number"]) + (" [" + label + "]" if label else "")


def signal_text(payload, current=True):
    lines = [f"Security schedule - Sunday {payload['local_date']}",
             f"Version {payload['publication_number']}" + (" - AMENDED" if payload["base_version_id"] else ""), payload["timezone"]]
    if not current:
        lines.append("SUPERSEDED - a newer published version exists. Confirm before sharing.")
    for service in (1, 2):
        lines += ["", payload["config"][f"service_{service}_label"] + (f" ({payload['config'][f'service_{service}_time']})" if payload["config"].get(f"service_{service}_time") else "")]
        lines.extend(f"{position_text(payload, s)}. {slot_text(s)}" for s in payload["slots"] if s["service_number"] == service)
    return "\n".join(lines)


def pdf_bytes(payload, current=True):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

    with FONT_LOCK:
        if "ScheduleUnicode" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont("ScheduleUnicode", str(Path(__file__).parent / "fonts" / "DejaVuSans.ttf")))
    normal = ParagraphStyle("schedule", fontName="ScheduleUnicode", fontSize=10, leading=15, splitLongWords=True)
    heading = ParagraphStyle("heading", parent=normal, fontSize=15, leading=21, spaceAfter=12)

    def paragraph(text, style=normal):
        # Escape *before* Paragraph parses markup: no images, links, file paths or
        # remote resources can become renderer instructions, even in hostile labels.
        rtl = next((unicodedata.bidirectional(c) in ("R", "AL") for c in text if unicodedata.bidirectional(c) in ("L", "R", "AL")), False)
        if any(unicodedata.bidirectional(c) in ("R", "AL") for c in text):
            from arabic_reshaper import reshape
            from bidi.algorithm import get_display
            text = get_display(reshape(text))
        if rtl:
            style = ParagraphStyle("rtl", parent=style, alignment=TA_RIGHT)
        return Paragraph(escape(text), style)

    output = io.BytesIO()
    doc = SimpleDocTemplate(output, pagesize=A4, leftMargin=36, rightMargin=36, topMargin=30, bottomMargin=36,
                            title=f"Security schedule {payload['local_date']} version {payload['publication_number']}", author="Church Security Scheduler")
    story = [paragraph(f"Sunday {payload['local_date']}", heading), paragraph(f"Version {payload['publication_number']}" + (" - AMENDED" if payload["base_version_id"] else "") + " | " + payload["timezone"]), Spacer(1, 10)]
    if not current:
        story += [paragraph("SUPERSEDED - a newer publication exists. Confirm before sharing."), Spacer(1, 10)]
    for service in (1, 2):
        title = payload["config"][f"service_{service}_label"]
        if payload["config"].get(f"service_{service}_time"):
            title += " - " + payload["config"][f"service_{service}_time"]
        rows = [[paragraph("Position"), paragraph(title)]]
        rows += [[paragraph(position_text(payload, s)), paragraph(slot_text(s))] for s in payload["slots"] if s["service_number"] == service]
        position_width = 130 if "inside_zone_label" in payload["config"] else 60
        table = Table(rows, colWidths=[position_width, A4[0]-72-position_width], repeatRows=1, hAlign="LEFT")
        table.setStyle(TableStyle([("VALIGN", (0,0), (-1,-1), "TOP"), ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#e8edf2")), ("LINEBELOW", (0,0), (-1,-1), .3, colors.HexColor("#aab5c0")), ("TOPPADDING", (0,0), (-1,-1), 6), ("BOTTOMPADDING", (0,0), (-1,-1), 6)]))
        story += [table, Spacer(1, 16)]

    def footer(canvas, document):
        canvas.setFont("ScheduleUnicode", 8)
        canvas.drawString(36, 19, f"{payload['local_date']} | Version {payload['publication_number']} | Page {document.page}")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()
