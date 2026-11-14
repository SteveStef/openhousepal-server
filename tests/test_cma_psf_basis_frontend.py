"""CMA test 11/13 (701 Sturbridge Dr) - every $/sq ft on a CMA uses ABOVE-GRADE finished area.

The analysis step's "Price Per Square Foot" chart and the report's market numbers
(lib/cma.ts computeMarketMetrics -> median/low/high $/sq ft on the client page and PDF) divided
comps by TOTAL finished area (incl. a finished basement) while the subject and the comparison table
used above-grade: comp #2 read $282 in the chart and $307 in the table beside it.
The lib test bundles the real frontend module with esbuild and runs it in Node (skipped if either
is missing)."""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def _run_ts(source: str) -> str:
    esbuild = FRONTEND / "node_modules" / ".bin" / ("esbuild.cmd" if shutil.which("cmd") else "esbuild")
    if not esbuild.exists() or not shutil.which("node"):
        pytest.skip("esbuild/node not available")
    with tempfile.TemporaryDirectory() as tmp:
        entry, out = Path(tmp) / "entry.ts", Path(tmp) / "out.js"
        entry.write_text(source, encoding="utf-8")
        subprocess.run([str(esbuild), str(entry), "--bundle", "--platform=node", "--format=cjs",
                        f"--tsconfig={FRONTEND / 'tsconfig.json'}", "--log-level=error", f"--outfile={out}"],
                       cwd=FRONTEND, check=True, capture_output=True, timeout=120)
        return subprocess.run(["node", str(out)], check=True, capture_output=True, text=True, timeout=60).stdout


def test_market_metrics_price_per_sqft_is_above_grade():
    out = _run_ts("""
import { computeMarketMetrics } from '@/lib/cma'
const comp = (p: number, ag: number, total: number) => ({ id: String(p), sale_price: p, status: 'sold', price_verified: true,
  is_included: true, valuation_role: 'primary', strength: 'strong', above_grade_finished_area: ag, sqft: total } as any)
console.log(JSON.stringify(computeMarketMetrics([comp(1380000, 4493, 4893), comp(1475000, 3434, 4234), comp(1810000, 3746, 3746)])))
""")
    m = json.loads(out)
    assert (m["psfMin"], m["psfMedian"], m["psfMax"]) == (307, 430, 483)


def test_analysis_chart_uses_the_same_basis_as_the_table():
    page = (FRONTEND / "app" / "studio" / "cma" / "[id]" / "page.tsx").read_text(encoding="utf-8")
    chart = page[page.index("function PsfChart"):page.index("function PsfChart") + 900]
    assert "pricePerSqft(effectiveCompPrice(c), agSqft(c))" in chart
    assert "c.sqft)" not in chart
