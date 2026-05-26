"""
Refresh config/data/hgnc_aliases.tsv from the HGNC complete set.

Run on a machine with network access:  make fetch-hgnc  (or: python scripts/fetch_hgnc.py)

Downloads the HGNC complete-set TSV and expands it into our 2-column
`alias<TAB>approved_symbol` table — every approved symbol maps to itself, and
every `alias_symbol` / `prev_symbol` maps to the approved symbol. This is the
table `preprocess/hgnc_resolver.py` loads at runtime (no network at runtime).

Safety: if the download fails (offline, blocked), the existing seed table is
LEFT IN PLACE and the script exits 0 with a warning — never clobbers the seed
with a partial/empty file.
"""

from __future__ import annotations

import sys
from pathlib import Path

# HGNC publishes the complete set as a public TSV.
HGNC_URL = (
    "https://storage.googleapis.com/public-download-files/hgnc/tsv/tsv/"
    "hgnc_complete_set.txt"
)
OUT = Path("config/data/hgnc_aliases.tsv")


def _download(url: str, timeout: int = 60) -> str:
    import urllib.request
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
        return resp.read().decode("utf-8", errors="replace")


def _expand(tsv_text: str) -> dict[str, str]:
    """Parse the HGNC complete set → {alias_or_symbol: approved_symbol}."""
    lines = tsv_text.splitlines()
    header = lines[0].split("\t")
    idx = {name: i for i, name in enumerate(header)}
    sym_i = idx.get("symbol")
    alias_i = idx.get("alias_symbol")
    prev_i = idx.get("prev_symbol")
    status_i = idx.get("status")
    if sym_i is None:
        raise ValueError("HGNC TSV has no 'symbol' column — format changed?")

    out: dict[str, str] = {}
    for line in lines[1:]:
        cols = line.split("\t")
        if len(cols) <= sym_i:
            continue
        if status_i is not None and len(cols) > status_i and cols[status_i] != "Approved":
            continue
        symbol = cols[sym_i].strip()
        if not symbol:
            continue
        out[symbol] = symbol
        for col_i in (alias_i, prev_i):
            if col_i is not None and len(cols) > col_i and cols[col_i]:
                for a in cols[col_i].strip().strip('"').split("|"):
                    a = a.strip()
                    if a:
                        out.setdefault(a, symbol)
    return out


def main() -> int:
    try:
        raw = _download(HGNC_URL)
        table = _expand(raw)
    except Exception as exc:  # noqa: BLE001
        print(f"[fetch-hgnc] WARNING: download/parse failed ({exc}). "
              f"Keeping existing seed at {OUT}.")
        return 0

    if len(table) < 1000:  # sanity — the real set has ~40k+ entries
        print(f"[fetch-hgnc] WARNING: only {len(table)} entries parsed; "
              f"refusing to overwrite the seed.")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        f.write("alias\tapproved_symbol\n")
        for alias, symbol in sorted(table.items()):
            f.write(f"{alias}\t{symbol}\n")
    print(f"[fetch-hgnc] wrote {len(table)} alias rows → {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
