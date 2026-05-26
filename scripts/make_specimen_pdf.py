#!/usr/bin/env python3
"""
Generate the synthetic multi-specimen surgical-pathology PDF for Phase-2b
validation, from the source text embedded in ground_truth/specimen_demo.json
(`_synthetic_source_text`). Fictional content — no PHI.

Usage:
    python scripts/make_specimen_pdf.py [OUTPUT.pdf]
    (default: data/actual_docs/specimen_demo.pdf)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
GT = _REPO / "ground_truth" / "specimen_demo.json"


def main(argv: list[str]) -> int:
    out = Path(argv[0]) if argv else (_REPO.parent / "data" / "actual_docs" / "specimen_demo.pdf")
    text = json.loads(GT.read_text(encoding="utf-8")).get("_synthetic_source_text", "")
    if not text:
        print("ERROR: no _synthetic_source_text in ground_truth/specimen_demo.json", file=sys.stderr)
        return 2

    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

    out.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["Normal"], fontSize=9.5, leading=13)
    doc = SimpleDocTemplate(str(out), pagesize=LETTER,
                            leftMargin=0.8 * inch, rightMargin=0.8 * inch,
                            topMargin=0.8 * inch, bottomMargin=0.8 * inch)
    flow = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            flow.append(Spacer(1, 6))
            continue
        # crude bold for section headers ending in ':'
        if line.endswith(":") or line.endswith("Report") or line.endswith("Diagnosis:"):
            flow.append(Paragraph(f"<b>{line}</b>", body))
        else:
            flow.append(Paragraph(line.replace("&", "&amp;").replace("<", "&lt;"), body))
    doc.build(flow)
    print(f"wrote {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
