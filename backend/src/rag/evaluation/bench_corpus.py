"""Five document kinds with known answers, for measuring what each parsing route does to retrieval:

1 digital prose PDF - 2 scanned prose (skewed, noisy, JPEG) - 3 native PDF tables - 4 a table pasted as a picture
into a text PDF - 5 a photographed inspection form (perspective, uneven light, blur).

build(folder) writes the files and returns [(file, question, [facts the retrieved chunk must contain])].
Used by tests/integration/test_document_kinds.py. Needs reportlab, Pillow, OpenCV and numpy (dev and ocr extras).
"""

from __future__ import annotations

import io
import random
import textwrap
from pathlib import Path

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def build(out: Path) -> list[tuple[str, str, list[str]]]:
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Image as RLImage
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    OUT = Path(out)
    OUT.mkdir(parents=True, exist_ok=True)
    ss = getSampleStyleSheet()
    random.seed(7)
    np.random.seed(7)

    # ---------- 1 digital prose ----------
    spec = [
     ("1. Concrete", "Structural concrete for columns on Level 3 shall be grade C40/50 with a maximum water-cement ratio of 0.42. Slabs on grade shall be C32/40. Concrete shall be supplied by a plant certified to ISO 9001 and delivered within 90 minutes of batching."),
     ("2. Hot weather concreting", "When the ambient temperature exceeds 38 degrees Celsius, the placing temperature of concrete shall not exceed 32 degrees Celsius. Ice or chilled water shall be used in the mix and curing shall begin within 20 minutes of finishing."),
     ("3. Reinforcement cover", "Nominal concrete cover to reinforcement shall be 40 mm for columns, 75 mm for foundations in contact with ground, and 25 mm for internal slabs. Spacers shall be of approved plastic or concrete type at 800 mm centres."),
     ("4. Testing", "Cube samples shall be taken at a rate of one set of four cubes per 50 cubic metres. Cubes shall be tested at 7 and 28 days. The engineer shall be notified 48 hours before any pour exceeding 120 cubic metres."),
    ]
    doc = SimpleDocTemplate(str(OUT/"1_spec_digital.pdf"), pagesize=A4)
    st = [Paragraph("Specification Section 03 30 00 - Cast-in-place Concrete", ss["Title"])]
    for h, t in spec: st += [Paragraph(h, ss["Heading2"]), Paragraph(t, ss["BodyText"]), Spacer(1, 8)]
    doc.build(st)

    # ---------- 2 scanned prose ----------
    minutes = ["Minutes of Meeting 22 - Site Progress", "",
     "Date: 12 March 2026   Chair: Karim Haddad", "",
     "1. The Level 5 slab pour is rescheduled to 24 March 2026 because of crane maintenance.",
     "2. Contractor to submit the revised temporary works design for the Level 5 props by 18 March.",
     "3. Façade mock-up inspection will take place on 2 April 2026 at 10:30 with the architect.",
     "4. Delay claim DC-077 for 6 days was rejected; the engineer noted the weather records do not support it.",
     "5. Safety: two near-miss reports were closed. Scaffold tag inspection every 7 days is mandatory."]
    def render_lines(lines, size=(1240, 1754)):
        im = Image.new("L", size, 255); d = ImageDraw.Draw(im); y = 120
        for i, l in enumerate(lines):
            f = ImageFont.truetype(BOLD if i == 0 else FONT, 34 if i == 0 else 27)
            for part in (textwrap.wrap(l, 62) or [""]):
                d.text((100, y), part, font=f, fill=20); y += 46
            y += 10
        return im
    def degrade(im, angle=1.2, q=55, noise=14):
        a = np.array(im.convert("L")); h, w = a.shape
        M = cv2.getRotationMatrix2D((w/2, h/2), angle, 1.0); a = cv2.warpAffine(a, M, (w, h), borderValue=235)
        a = (a.astype(np.float32) + np.random.normal(0, noise, a.shape)).clip(0, 255).astype(np.uint8)
        for _ in range(60): cv2.circle(a, (random.randrange(w), random.randrange(h)), random.randrange(1, 3), 120, -1)
        a = cv2.GaussianBlur(a, (3, 3), 0.8)
        buf = io.BytesIO(); Image.fromarray(a).save(buf, "JPEG", quality=q); buf.seek(0); return Image.open(buf).convert("RGB")
    degrade(render_lines(minutes)).save(OUT/"2_scanned_minutes.pdf", "PDF", resolution=150)

    # ---------- 3 native tables ----------
    boq = [["Item", "Description", "Unit", "Quantity", "Rate (AED)", "Amount (AED)"],
     ["3.01", "Reinforcement steel 16 mm", "tonne", "420", "3,150", "1,323,000"],
     ["3.02", "Reinforcement steel 25 mm", "tonne", "1,285", "3,180", "4,086,300"],
     ["3.03", "Concrete C40/50", "m3", "6,940", "385", "2,671,900"],
     ["3.04", "Formwork to columns", "m2", "9,120", "62", "565,440"],
     ["3.05", "Waterproofing membrane", "m2", "3,775", "48", "181,200"]]
    pay = [["Milestone", "Percentage", "Amount (AED)", "Due"],
     ["Advance payment", "10%", "2,400,000", "Within 14 days of signing"],
     ["Foundation complete", "20%", "4,800,000", "30 June 2026"],
     ["Structure topped out", "30%", "7,200,000", "31 December 2026"],
     ["MEP first fix complete", "25%", "6,000,000", "30 April 2027"],
     ["Handover", "15%", "3,600,000", "30 September 2027"]]
    def tbl(data, widths):
        t = Table(data, colWidths=widths); t.setStyle(TableStyle([("GRID", (0,0), (-1,-1), 0.5, colors.black), ("BACKGROUND", (0,0), (-1,0), colors.lightgrey), ("FONTSIZE", (0,0), (-1,-1), 9)])); return t
    SimpleDocTemplate(str(OUT/"3_tables_native.pdf"), pagesize=A4).build([
     Paragraph("Bill of Quantities - Structural Works", ss["Heading1"]), tbl(boq, [40, 170, 45, 60, 65, 80]), Spacer(1, 24),
     Paragraph("Payment Schedule", ss["Heading1"]), tbl(pay, [130, 70, 90, 170])])

    # ---------- 4 table as an image inside a digital PDF ----------
    doors = [["Door", "Location", "Type", "Fire rating", "Width x Height (mm)"],
     ["D-101", "Main lobby", "Double glazed", "None", "1800 x 2400"],
     ["D-214", "Stair core B", "Steel", "120 min", "1000 x 2100"],
     ["D-307", "Plant room", "Louvred steel", "60 min", "1200 x 2100"],
     ["D-415", "Server room", "Steel", "90 min", "900 x 2100"],
     ["D-520", "Roof access", "Steel", "30 min", "900 x 2000"]]
    W, Hh = 1100, 60 * len(doors) + 20
    im = Image.new("RGB", (W, Hh), "white"); d = ImageDraw.Draw(im); cx = [10, 160, 400, 600, 780, W - 10]
    for r, row in enumerate(doors):
        y = 10 + r * 60; d.line([(10, y), (W - 10, y)], fill="black", width=2)
        for c, v in enumerate(row): d.text((cx[c] + 8, y + 16), v, font=ImageFont.truetype(BOLD if r == 0 else FONT, 24), fill="black")
    d.line([(10, Hh - 10), (W - 10, Hh - 10)], fill="black", width=2)
    for x in cx: d.line([(x, 10), (x, Hh - 10)], fill="black", width=2)
    im.save(OUT/"_doors.png")
    SimpleDocTemplate(str(OUT/"4_table_as_image.pdf"), pagesize=A4).build([
     Paragraph("Door Schedule - Level 2 to Level 5", ss["Heading1"]),
     Paragraph("The schedule below lists fire-rated doors. Refer to drawing A-410 for locations. All fire doors shall carry third-party certification.", ss["BodyText"]),
     Spacer(1, 12), RLImage(str(OUT/"_doors.png"), width=480, height=480*Hh/W)])

    # ---------- 5 photographed form ----------
    form = [["Check", "Result", "Remark"], ["Scaffold tags current", "PASS", "Tag 7731 valid"], ["Edge protection", "FAIL", "Missing at gridline C4"],
            ["Fire extinguisher", "PASS", "Serial FE-0912"], ["Permit to work displayed", "PASS", "PTW-4471"], ["Housekeeping", "FAIL", "Debris on Level 4 walkway"]]
    im = Image.new("RGB", (1200, 900), (250, 250, 245)); d = ImageDraw.Draw(im)
    d.text((60, 40), "SITE INSPECTION REPORT  SIR-0518", font=ImageFont.truetype(BOLD, 40), fill="black")
    d.text((60, 100), "Inspector: Nadia Farouk    Date: 3 April 2026", font=ImageFont.truetype(FONT, 28), fill="black")
    cx = [60, 520, 760, 1140]
    for r, row in enumerate(form):
        y = 180 + r * 80; d.line([(60, y), (1140, y)], fill="black", width=3)
        for c, v in enumerate(row): d.text((cx[c] + 10, y + 22), v, font=ImageFont.truetype(BOLD if r == 0 else FONT, 28), fill="black")
    d.line([(60, 180 + 6*80), (1140, 180 + 6*80)], fill="black", width=3)
    for x in cx: d.line([(x, 180), (x, 180 + 6*80)], fill="black", width=3)
    a = np.array(im); h, w = a.shape[:2]
    src = np.float32([[0,0],[w,0],[w,h],[0,h]]); dst = np.float32([[40,25],[w-20,0],[w-55,h-30],[15,h-5]])
    a = cv2.warpPerspective(a, cv2.getPerspectiveTransform(src, dst), (w, h), borderValue=(200,200,195))
    grad = np.tile(np.linspace(0.75, 1.05, w), (h, 1))[..., None]; a = (a * grad).clip(0, 255).astype(np.uint8)
    a = cv2.GaussianBlur(a, (5, 5), 1.2); a = (a.astype(np.float32) + np.random.normal(0, 8, a.shape)).clip(0, 255).astype(np.uint8)
    Image.fromarray(a).save(OUT/"5_inspection_photo.jpg", quality=70)

    # ---------- ground truth: question, file, facts that the retrieved chunk must contain ----------
    Q = [
     ("1_spec_digital.pdf", "What concrete grade is required for columns on Level 3?", ["C40/50"]),
     ("1_spec_digital.pdf", "What is the maximum placing temperature in hot weather?", ["32 degrees"]),
     ("1_spec_digital.pdf", "What cover is required for foundations in contact with ground?", ["75 mm"]),
     ("1_spec_digital.pdf", "How many cubes are taken per 50 cubic metres?", ["four cubes"]),
     ("2_scanned_minutes.pdf", "When is the Level 5 slab pour rescheduled to?", ["24 March 2026"]),
     ("2_scanned_minutes.pdf", "What happened to delay claim DC-077?", ["rejected"]),
     ("2_scanned_minutes.pdf", "When is the façade mock-up inspection?", ["2 April 2026"]),
     ("2_scanned_minutes.pdf", "How often must scaffold tags be inspected?", ["7 days"]),
     ("3_tables_native.pdf", "What quantity of 25 mm reinforcement steel is in the bill of quantities?", ["1,285"]),
     ("3_tables_native.pdf", "What is the rate for waterproofing membrane?", ["3,775", "48"]),
     ("3_tables_native.pdf", "How much is due when the structure is topped out?", ["7,200,000"]),
     ("3_tables_native.pdf", "When is the handover payment due?", ["30 September 2027"]),
     ("4_table_as_image.pdf", "What fire rating does door D-214 have?", ["D-214", "120 min"]),
     ("4_table_as_image.pdf", "Which door is in the server room?", ["D-415"]),
     ("4_table_as_image.pdf", "What is the width and height of door D-520?", ["900 x 2000"]),
     ("4_table_as_image.pdf", "Which door is in the plant room and what type is it?", ["D-307", "Louvred"]),
     ("5_inspection_photo.jpg", "Where is edge protection missing?", ["C4"]),
     ("5_inspection_photo.jpg", "What is the permit to work number?", ["4471"]),
     ("5_inspection_photo.jpg", "Which item failed because of debris?", ["Debris"]),
     ("5_inspection_photo.jpg", "What is the scaffold tag number?", ["7731"]),
    ]
    return Q
