"""Render the license agreement's pages to PNGs for the in-browser viewer.

Run after replacing the agreement PDF (developer machine only):
    pip install pypdfium2
    python backend/scripts/render_agreement_pages.py v1

Writes frontend/legal/<version>/page-N.png. The signing page shows these
images, with the signer's details overlaid live, instead of shipping a PDF
viewer; the signed PDF itself is produced on the server from the original.
"""

import json
import sys
from pathlib import Path

import pypdfium2 as pdfium

ROOT = Path(__file__).resolve().parents[2]
version = sys.argv[1] if len(sys.argv) > 1 else "v1"
meta = json.loads((ROOT / "backend/autorack/legal" / f"license-agreement-{version}.json").read_text())
pdf = pdfium.PdfDocument(str(ROOT / "backend/autorack/legal" / meta["file"]))
out = ROOT / "frontend/legal" / version
out.mkdir(parents=True, exist_ok=True)
for i in range(len(pdf)):
    # Text only: 16 grey levels keeps each page ~60 KB and still crisp.
    image = pdf[i].render(scale=2).to_pil().convert("L").quantize(colors=16)
    image.save(out / f"page-{i + 1}.png", optimize=True)
print(f"wrote {len(pdf)} pages to {out}")
