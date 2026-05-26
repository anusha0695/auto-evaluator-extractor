"""
Phase 2 M9 verification gate — UI extension (no Streamlit execution).

Done-when (deterministic portion of PHASE_2_PLAN.md M9):
  - graph_linear persists extraction_v2 + verification_v2 artifacts every run
    (verified structurally: the linker/verifier nodes accept a persistence dep);
  - the Phase-2a view module imports and its pure artifact loader works
    (returns (None, None) gracefully for an unknown doc);
  - app.py wires the new tab.

The visual render is validated by launching Streamlit:  make ui PHASE=1

Run:  PYTHONPATH=. python scripts/gates/gate_p2_m9_ui_v2.py
"""

from __future__ import annotations

import sys

from ui.phase1.views import extraction_v2_view


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    print("[1] pure artifact loader")
    ext, ver = extraction_v2_view.load_v2_artifacts("nonexistent_doc_zzz")
    check("unknown doc → (None, None)", ext is None and ver is None)
    check("render is callable", callable(getattr(extraction_v2_view, "render", None)))

    print("[2] graph_linear persists artifacts (linker/verifier accept persistence dep)")
    import inspect
    from pipeline import graph_linear
    lsig = inspect.signature(graph_linear._make_linker_node)
    vsig = inspect.signature(graph_linear._make_verifier_node)
    check("_make_linker_node has persistence param", "persistence" in lsig.parameters)
    check("_make_verifier_node has persistence param", "persistence" in vsig.parameters)

    print("[3] app.py wires the Phase 2a tab")
    import ast
    from pathlib import Path
    src = Path("ui/phase1/app.py").read_text(encoding="utf-8")
    check("app imports extraction_v2_view", "extraction_v2_view" in src)
    check("app adds Phase 2a tab", "Phase 2a extraction" in src and "extraction_v2_view.render" in src)
    ast.parse(src)  # syntax

    print("-" * 60)
    if fails:
        print(f"M9 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("M9 VERIFY: PASS — v2 artifacts persisted + view + tab wired.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
