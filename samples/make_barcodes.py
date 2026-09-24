# Regenerates samples/04-busy-day-barcodes.pdf from 04-busy-day-300-orders.csv.
# Needs: pip install reportlab   Run from anywhere: python samples/make_barcodes.py
import csv
from pathlib import Path
from collections import defaultdict
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.graphics.barcode import createBarcodeDrawing
from reportlab.graphics import renderPDF
from reportlab.lib.colors import HexColor

NAVY = HexColor("#162238"); BLUE = HexColor("#3e7bfa"); GREY = HexColor("#6b7486"); LINE = HexColor("#c9ced6")
HERE = Path(__file__).resolve().parent
SRC = str(HERE / "04-busy-day-300-orders.csv")
OUT = str(HERE / "04-busy-day-barcodes.pdf")
W, H = letter
M = 36

rows = list(csv.DictReader(open(SRC)))
products = {}
uses = defaultdict(set)
orders = []
for r in rows:
    products.setdefault(r["barcode"], r)
    uses[r["barcode"]].add(r["order_number"])
    if r["order_number"] not in orders:
        orders.append(r["order_number"])
products = sorted(products.values(), key=lambda r: (r["location"], r["sku"]))

def check(body):
    t = sum(int(ch) * (3 if i % 2 == 0 else 1) for i, ch in enumerate(reversed(body)))
    return str((10 - t % 10) % 10)
DECOYS = [("99999999999" + check("99999999999"), "Decoy: not on any order"),
          ("08888888888" + check("08888888888"), "Decoy: not on any order"),
          ("07777777777" + check("07777777777"), "Decoy: not on any order")]
assert not {d for d, _ in DECOYS} & {p["barcode"] for p in products}

c = canvas.Canvas(OUT, pagesize=letter)
c.setTitle("Autorack test barcodes: busy day (300 orders)")
c.setAuthor("Autorack")

def header(title, sub):
    c.setFillColor(NAVY); c.rect(0, H - 54, W, 54, stroke=0, fill=1)
    c.setFillColor(HexColor("#ffffff")); c.setFont("Helvetica-Bold", 13); c.drawString(M, H - 26, "AUTORACK")
    c.setFillColor(BLUE); c.setFont("Helvetica-Bold", 6.5); c.drawString(M, H - 37, "V E R I F I E D   L O G I S T I C S")
    c.setFillColor(HexColor("#ffffff")); c.setFont("Helvetica-Bold", 12); c.drawRightString(W - M, H - 26, title)
    c.setFillColor(HexColor("#aab4c5")); c.setFont("Helvetica", 8.5); c.drawRightString(W - M, H - 39, sub)

def footer(n):
    c.setFillColor(GREY); c.setFont("Helvetica", 7.5)
    c.drawString(M, 22, "Print at 100% / Actual size (not 'Fit to page'). Test data only: made-up products.")
    c.drawRightString(W - M, 22, f"Page {n}")

prod_pages = -(-(len(products) + len(DECOYS)) // 10)
ord_first = 2 + prod_pages
ord_last = ord_first - 1 + -(-len(orders) // 24)
page = 1
# ---- Cover
header("Test barcodes", "for samples/04-busy-day-300-orders.csv")
y = H - 100
c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 20); c.drawString(M, y, "How to use these barcodes"); y -= 30
steps = [
    "1. In the dashboard, import 04-busy-day-300-orders.csv (Orders > Import CSV).",
    "2. Print this file at 100% scale. You can also scan straight off a screen.",
    f"3. On a linked phone, sign in and tap 'Scan pick sheet'. Scan an ORDER label (pages {ord_first}-{ord_last})",
    "   to open that order, e.g. WO-20001. The order-number barcode opens the order directly.",
    f"4. Tap Scan and scan PRODUCT labels (pages 2-{1 + prod_pages}). Each order's items are listed on the phone;",
    "   find the matching labels by name, SKU or bin. Scan one label several times for quantity > 1.",
    f"5. Scan a DECOY label (end of page {1 + prod_pages}) to see the red WRONG ITEM screen.",
    "6. Scan a product that isn't on the open order: also WRONG ITEM. Scan one too many times:",
    "   ALREADY HAVE ENOUGH.",
]
c.setFont("Helvetica", 10.5); c.setFillColor(HexColor("#222b3a"))
for s in steps:
    c.drawString(M, y, s); y -= 17
y -= 10
c.setFont("Helvetica-Bold", 11); c.setFillColor(NAVY); c.drawString(M, y, "What's inside"); y -= 18
c.setFont("Helvetica", 10.5); c.setFillColor(HexColor("#222b3a"))
for s in [f"Pages 2-{1 + prod_pages}: {len(products)} product labels (UPC-A), sorted by bin location, plus {len(DECOYS)} decoys.",
          f"Pages {ord_first}-{ord_last}: {len(orders)} order-number labels (Code 128), WO-20001 to WO-20300.",
          "All 300 orders use only these 30 products, so 30 product labels cover every pick."]:
    c.drawString(M, y, s); y -= 17
y -= 10
c.setFont("Helvetica-Bold", 11); c.setFillColor(NAVY); c.drawString(M, y, "Tips"); y -= 18
c.setFont("Helvetica", 10.5); c.setFillColor(HexColor("#222b3a"))
for s in ["Hold the phone 10-20 cm away; tilt slightly if there's glare.",
          "Cut the product labels out and tape them to bins/boxes for a realistic walk-through.",
          "A USB or Bluetooth scanner works too: the phone app accepts it as keyboard input."]:
    c.drawString(M, y, s); y -= 17
footer(page); c.showPage(); page += 1

# ---- Product labels: 2 x 5 per page
cols, rws = 2, 5
lw, lh = (W - 2 * M) / cols, (H - 54 - 30 - 40) / rws
items = [(p["barcode"], p["description"], f'{p["sku"]}  ·  Bin {p["location"]}', f'on {len(uses[p["barcode"]])} orders') for p in products]
items += [(d, label, "Scan to test WRONG ITEM", "") for d, label in DECOYS]
for start in range(0, len(items), cols * rws):
    header("Product labels", "Scan these as the items you pick")
    for i, (code, name, meta, note) in enumerate(items[start:start + cols * rws]):
        col, row = i % cols, i // cols
        x0 = M + col * lw; y0 = H - 54 - 30 - (row + 1) * lh
        c.setStrokeColor(LINE); c.setDash(3, 3); c.rect(x0 + 4, y0 + 4, lw - 8, lh - 8); c.setDash()
        decoy = name.startswith("Decoy")
        c.setFillColor(HexColor("#c62828") if decoy else NAVY); c.setFont("Helvetica-Bold", 11)
        c.drawString(x0 + 16, y0 + lh - 24, name[:40])
        c.setFillColor(GREY); c.setFont("Helvetica", 8.5); c.drawString(x0 + 16, y0 + lh - 37, meta)
        if note: c.drawRightString(x0 + lw - 16, y0 + lh - 37, note)
        d = createBarcodeDrawing("UPCA", value=code[:11], barWidth=1.35, barHeight=58, humanReadable=True, fontSize=9)
        renderPDF.draw(d, c, x0 + (lw - d.width) / 2, y0 + 16)
    footer(page); c.showPage(); page += 1

# ---- Order labels: 3 x 8 per page
cols, rws = 3, 8
lw, lh = (W - 2 * M) / cols, (H - 54 - 30 - 40) / rws
for start in range(0, len(orders), cols * rws):
    header("Order labels", "Scan on the phone's order list to open the order")
    for i, num in enumerate(orders[start:start + cols * rws]):
        col, row = i % cols, i // cols
        x0 = M + col * lw; y0 = H - 54 - 30 - (row + 1) * lh
        c.setStrokeColor(LINE); c.setDash(3, 3); c.rect(x0 + 3, y0 + 3, lw - 6, lh - 6); c.setDash()
        n_lines = sum(1 for r in rows if r["order_number"] == num)
        c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 10); c.drawString(x0 + 12, y0 + lh - 18, num)
        c.setFillColor(GREY); c.setFont("Helvetica", 7.5); c.drawRightString(x0 + lw - 12, y0 + lh - 18, f"{n_lines} line{'s' if n_lines != 1 else ''}")
        d = createBarcodeDrawing("Code128", value=num, barWidth=1.15, barHeight=34, humanReadable=False, quiet=True)
        renderPDF.draw(d, c, x0 + (lw - d.width) / 2, y0 + 12)
    footer(page); c.showPage(); page += 1

c.save()
print("pages", page - 1, "products", len(products), "orders", len(orders))
