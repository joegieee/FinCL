"""§5.5/§9.3 performance budget: p95 latency for a composite money-flow read.

Measures two readers, run as::

    uv run python scripts/bench_money_flow.py

1. :func:`fincl.forecast.cash_timeline` — the reader §5.5 names as "the
   expensive input" and the widest fan-out in the money-flow call graph
   (``docs/M3A-money-flow-call-graph.md`` §4.2: 160 of the 200 enumerated
   functions are reachable from it). M3a's acceptance required this be
   measured "before the UI exists and while the reader seam is the only
   variable" — kept here as the original M3a evidence.
2. :func:`fincl.money_flow.money_flow_page` — the actual composite Overview
   reader (M3b.3 step 09/11), which additionally composes the reported
   -Analysis economic-flow/burn read and the position-cutoff read under one
   pinned generation. This is the number Release A's acceptance (plan §7
   M3b.3, "Measure the assembled endpoint, not only the forecast reader")
   is actually about.

Budget (§5.5, plan §7 M3b.3 item 6): warm p95 under 800 ms, first read under
2 s, on the largest ledger available. This script times a synthetic ledger
built the same way ``scripts/create_demo.py`` builds its demo data, scaled up to
``MONTHS`` months of activity. It never migrates or writes to an existing
ledger (the execution contract forbids that as a side effect of a benchmark);
if one exists at ``var/fincl.sqlite3`` it is only noted in the output, not
timed. The synthetic numbers are therefore a floor, not a substitute for
measuring a real ledger opened read-only at the current schema.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "tests"))

from fincl.document_service import DocumentImportService  # noqa: E402
from fincl.forecast import cash_timeline  # noqa: E402
from fincl.money_flow import money_flow_page  # noqa: E402
from fincl.refresh import refresh_ledger  # noqa: E402
from revolut_pdf import revolut_pdf_bytes  # noqa: E402

MONTHS = 36
WARM_ITERATIONS = 30
WARM_BUDGET_MS = 800.0
COLD_BUDGET_MS = 2000.0
_TODAY = date(2028, 1, 1)
_HISTORY_START = "2025-01-01"
_HISTORY_END = (_TODAY - timedelta(days=1)).isoformat()
_REAL_LEDGER = _REPO_ROOT / "var" / "fincl.sqlite3"

_MONTHLY_SPEND = [
    (2, "Apartment rent", "-1500.00"),
    (4, "Acme Streaming", "-10.00"),
    (7, "Corner Grocer", "-90.00"),
    (10, "Mobile Plan", "-30.00"),
    (13, "Fresh Market", "-50.00"),
    (15, "Lunch Counter", "-15.00"),
    (18, "Clothing Store", "-80.00"),
    (20, "Ride-hail", "-20.00"),
    (22, "Neighbourhood Bakery", "-15.00"),
    (24, "Corner Grocer", "-65.00"),
    (26, "Pharmacy", "-25.00"),
    (28, "Transit Top-up", "-40.00"),
]


def build_ledger(root: Path, *, months: int = MONTHS) -> Path:
    """A synthetic ledger scaled to ``months`` of salary + routine spending."""
    database = root / "bench.sqlite3"
    events: list[tuple[date, str, str, Decimal]] = []
    for offset in range(months):
        year, month = 2025 + offset // 12, offset % 12 + 1
        events.append((date(year, month, 1), "Topup", "Employer salary", Decimal("5000.00")))
        for day, merchant, amount in _MONTHLY_SPEND:
            events.append((date(year, month, day), "Card Payment", merchant, Decimal(amount)))

    rows = []
    balance = Decimal(0)
    for dt, kind, name, value in sorted(events):
        timestamp = f"{dt.isoformat()} 10:00:00"
        balance += value
        rows.append([
            kind, "Current", timestamp, timestamp, name,
            str(value), "0.00", "SGD", "COMPLETED", str(balance),
        ])

    source = root / "bench-revolut.pdf"
    source.write_bytes(revolut_pdf_bytes(rows))
    DocumentImportService.plaintext_phase2(database, root / "evidence").import_revolut_pdf(source)
    refresh_ledger(database)
    return database


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))
    return ordered[index]


def measure_warm(database: Path, *, iterations: int = WARM_ITERATIONS) -> list[float]:
    cash_timeline(database, today=_TODAY)  # untimed: settle import/interpreter overhead
    samples = []
    for _ in range(iterations):
        start = time.perf_counter()
        cash_timeline(database, today=_TODAY)
        samples.append(time.perf_counter() - start)
    return samples


def measure_warm_money_flow(database: Path, *, iterations: int = WARM_ITERATIONS) -> list[float]:
    """Warm p95 of the actual composite Overview reader, not just its forecast leg."""
    money_flow_page(
        database, today=_TODAY, history_start=_HISTORY_START, history_end=_HISTORY_END,
    )  # untimed: settle import/interpreter overhead
    samples = []
    for _ in range(iterations):
        start = time.perf_counter()
        money_flow_page(
            database, today=_TODAY, history_start=_HISTORY_START, history_end=_HISTORY_END,
        )
        samples.append(time.perf_counter() - start)
    return samples


_COLD_SNIPPET = """
import sys, time
from datetime import date
from pathlib import Path
import tempfile
sys.path.insert(0, {scripts_dir!r})
sys.path.insert(0, {tests_dir!r})
from bench_money_flow import build_ledger
from fincl.forecast import cash_timeline
database = build_ledger(Path(tempfile.mkdtemp()), months={months})
start = time.perf_counter()
cash_timeline(database, today=date(2028, 1, 1))
print(time.perf_counter() - start)
"""

_COLD_SNIPPET_MONEY_FLOW = """
import sys, time
from datetime import date
from pathlib import Path
import tempfile
sys.path.insert(0, {scripts_dir!r})
sys.path.insert(0, {tests_dir!r})
from bench_money_flow import build_ledger
from fincl.money_flow import money_flow_page
database = build_ledger(Path(tempfile.mkdtemp()), months={months})
start = time.perf_counter()
money_flow_page(database, today=date(2028, 1, 1), history_start={history_start!r}, history_end={history_end!r})
print(time.perf_counter() - start)
"""


def measure_cold(*, months: int = MONTHS) -> float:
    """A fresh interpreter, a freshly built ledger, one timed first call.

    This is the closest honest approximation of "cold" available without root
    (no page-cache drop): a brand new process and a ledger file this process
    has never touched, so nothing about this read is already resident.
    """
    snippet = _COLD_SNIPPET.format(
        scripts_dir=str(_REPO_ROOT / "scripts"),
        tests_dir=str(_REPO_ROOT / "tests"),
        months=months,
    )
    result = subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=_REPO_ROOT, capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip().splitlines()[-1])


def measure_cold_money_flow(*, months: int = MONTHS) -> float:
    """The composite reader's own first-read latency: a fresh process, a fresh
    ledger, one timed ``money_flow_page`` call — the number the plan's "first
    -read latency <2s" budget (§7 M3b.3 item 6) actually names, distinct from
    the forecast-only cold read above."""
    snippet = _COLD_SNIPPET_MONEY_FLOW.format(
        scripts_dir=str(_REPO_ROOT / "scripts"),
        tests_dir=str(_REPO_ROOT / "tests"),
        months=months,
        history_start=_HISTORY_START,
        history_end=_HISTORY_END,
    )
    result = subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=_REPO_ROOT, capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip().splitlines()[-1])


def _report(label: str, *, warm_samples: list[float], cold_seconds: float) -> None:
    warm_p95_ms = _p95(warm_samples) * 1000
    cold_ms = cold_seconds * 1000
    print(f"[{label}] warm p95 over {len(warm_samples)} calls: {warm_p95_ms:.1f} ms "
          f"(budget {WARM_BUDGET_MS:.0f} ms) "
          f"{'PASS' if warm_p95_ms < WARM_BUDGET_MS else 'FAIL'}")
    print(f"[{label}] first read (fresh process, fresh ledger, first call): {cold_ms:.1f} ms "
          f"(budget {COLD_BUDGET_MS:.0f} ms) "
          f"{'PASS' if cold_ms < COLD_BUDGET_MS else 'FAIL'}")


def main() -> None:
    root = Path(tempfile.mkdtemp())
    database = build_ledger(root)

    print(f"synthetic ledger: {MONTHS} months, {WARM_ITERATIONS} warm samples "
          f"(not a real user ledger)")
    if _REAL_LEDGER.exists():
        print(f"a ledger exists at {_REAL_LEDGER.relative_to(_REPO_ROOT)}; this script does not "
              f"time or modify it. Synthetic ledger only.")
    else:
        print("no existing ledger found; synthetic only.")

    _report(
        "cash_timeline (forecast leg only, M3a baseline)",
        warm_samples=measure_warm(database), cold_seconds=measure_cold(),
    )
    _report(
        "money_flow_page (composite Overview reader, Release A)",
        warm_samples=measure_warm_money_flow(database), cold_seconds=measure_cold_money_flow(),
    )


if __name__ == "__main__":
    main()
