"""Report exports (v0.7 slice 3).

An export is a saved report (or an unsaved definition) evaluated once, inside
one pinned read snapshot, and written as CSV, JSON or a self-contained HTML
page together with a manifest that says exactly what it is: schema, API and
build version, generation, lens, window, the date the figures were worked out,
the reporting currency and every exchange rate used.

What an export never contains: bank evidence files, the decision journal, or
anything outside the report's own rows. Those belong to the separate full
backup. Generated files are outputs, not authority — nothing reads them back.

Money stays exact decimal text. CSV text cells that a spreadsheet would run as
a formula are prefixed with an apostrophe; JSON stays lossless.
"""
from __future__ import annotations

import csv
import html
import io
import json
import re
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .. import __version__
from ..db import SCHEMA_VERSION, read_snapshot
from ..errors import FinCLError
from ..reporting_context import ReportedScope, build_reporting_context
from .definitions import COLUMNS, validate_definition
from .queries import _definitions, evaluate

EXPORT_FORMATS = ("csv", "json", "html")
EXPORT_VERSION = "report-export-v1"
API_VERSION = "v1"
# A report is a bounded aggregate; anything near this is a bug, not a report.
MAX_EXPORT_BYTES = 20 * 1024 * 1024
_MONEY_COLUMNS = ("gross_cost", "refunds", "net_cost")
_COLUMN_TITLE = {"gross_cost": "Spent", "refunds": "Refunds", "net_cost": "Net", "entry_count": "Transactions"}
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_CONTENT_TYPE = {"csv": "application/zip", "json": "application/json", "html": "text/html; charset=utf-8"}
_EXTENSION = {"csv": "zip", "json": "json", "html": "html"}


def _cell(value: Any) -> str:
    """A CSV text cell a spreadsheet will show, never run."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text


def filename(name: str, evaluated_on: str, fmt: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "report"
    return f"{slug}-{evaluated_on}.{_EXTENSION[fmt]}"


def _reported(amounts: list[dict[str, Any]]) -> dict[tuple[Any, ...], dict[str, Any]]:
    return {tuple(item["path"]): item["value"] for item in amounts}


def _reported_text(value: dict[str, Any] | None) -> str | None:
    if not value or value.get("unavailable") or not value.get("reported"):
        return None
    return str(value["reported"]["value"])


def build_export(database: Path, *, fmt: str, today: date, now: datetime, report_id: str | None = None,
                 definition: dict[str, Any] | None = None, expected: str | None = None,
                 custom_categories: frozenset[str] = frozenset()) -> tuple[str, str, bytes]:
    """(filename, content type, bytes) for one export, from one snapshot."""
    from ..reporting_workspaces import annotate_workspace

    if fmt not in EXPORT_FORMATS:
        raise FinCLError(f"The export format is one of {', '.join(EXPORT_FORMATS)}")
    if (report_id is None) == (definition is None):
        raise FinCLError("Export either a saved report or an unsaved definition, not both")
    with read_snapshot(database) as connection:
        chosen: dict[str, Any] | None = definition
        if definition is not None:
            validate_definition(definition, with_reason=False)
        else:
            chosen = next((d for d in _definitions(connection) if d["report_id"] == report_id), None)
        if chosen is None:
            raise FinCLError("that report does not exist")
        context = build_reporting_context(connection, scope=ReportedScope(valuation_as_of=today))
        result = evaluate(connection, chosen, today=today, custom_categories=custom_categories, expected=expected)
        workspace = annotate_workspace(result, context=context, resource="reports/export").model_dump(mode="json")
    reported = _reported(workspace["amounts"])
    rates = sorted({
        json.dumps(rate, sort_keys=True)
        for value in reported.values() for rate in (value.get("rate_path") or [])
    })
    manifest = {
        "export_version": EXPORT_VERSION,
        "exported_at": now.isoformat(timespec="seconds"),
        "app_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "api_version": API_VERSION,
        "format": fmt,
        "generation": result["generation"],
        "report": result["report"],
        "lens": result["lens"],
        "lens_note": result["lens_note"],
        "window": result["window"],
        "evaluated_on": result["evaluated_on"],
        "data_through": result["data_through"],
        "warnings": result["warnings"],
        "row_count": len(result["rows"]),
        "reporting_currency": workspace["reporting_currency"],
        "valuation_as_of": workspace["valuation_as_of"],
        "exchange_rates": [json.loads(rate) for rate in rates],
        "money": "exact decimal text in each row's own currency; reported columns are in the reporting currency",
        "contents": "this report's rows and totals only; no bank evidence, journal or other ledger data",
    }
    name = str(result["report"].get("name") or "report")
    columns = [column for column in COLUMNS if column in chosen["columns"]]
    rows = [{**row, "reported": {column: _reported_text(reported.get(("rows", index, column)))
                                  for column in _MONEY_COLUMNS}}
            for index, row in enumerate(result["rows"])]
    totals = {currency: {**total, "reported": {column: _reported_text(reported.get(("totals_by_currency", currency, column)))
                                                for column in _MONEY_COLUMNS}}
              for currency, total in result["totals_by_currency"].items()}
    for row in rows:
        row.pop("cost_parts", None)

    if fmt == "json":
        body = json.dumps({"manifest": manifest, "columns": columns, "rows": rows, "totals_by_currency": totals},
                          indent=2, ensure_ascii=False).encode()
    elif fmt == "csv":
        body = _zip(name, result, manifest, columns, rows, totals)
    else:
        body = _html(name, result, manifest, columns, rows, totals).encode()
    if len(body) > MAX_EXPORT_BYTES:
        raise FinCLError("This export is too large; narrow the report and try again")
    return filename(name, result["evaluated_on"], fmt), _CONTENT_TYPE[fmt], body


def _table(result: dict[str, Any], manifest: dict[str, Any], columns: list[str], rows: list[dict[str, Any]],
           totals: dict[str, dict[str, Any]]) -> tuple[list[str], list[list[str]]]:
    keys = list(result["report"].get("group_by") or [])
    reporting = manifest["reporting_currency"]
    money = [c for c in columns if c in _MONEY_COLUMNS]
    header = [result["group_labels"][key] for key in keys] + ["Currency"] \
        + [_COLUMN_TITLE[c] for c in columns] + [f"{_COLUMN_TITLE[c]} ({reporting})" for c in money]
    body: list[list[str]] = []
    for row in rows:
        body.append([g["label"] for g in row["group"]] + [row["currency"]]
                    + [str(row[c]) for c in columns] + [row["reported"][c] or "" for c in money])
    for currency, total in totals.items():
        body.append(["Total"] * len(keys) + [currency]
                    + [str(total[c]) for c in columns] + [total["reported"][c] or "" for c in money])
    return header, body


def _zip(name: str, result: dict[str, Any], manifest: dict[str, Any], columns: list[str],
         rows: list[dict[str, Any]], totals: dict[str, dict[str, Any]]) -> bytes:
    header, body = _table(result, manifest, columns, rows, totals)
    text = io.StringIO()
    writer = csv.writer(text, lineterminator="\r\n")
    writer.writerow([_cell(value) for value in header])
    for line in body:
        writer.writerow([_cell(value) for value in line])
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(filename(name, result["evaluated_on"], "csv").removesuffix(".zip") + ".csv",
                        "﻿" + text.getvalue())
        bundle.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
    return archive.getvalue()


def _html(name: str, result: dict[str, Any], manifest: dict[str, Any], columns: list[str],
          rows: list[dict[str, Any]], totals: dict[str, dict[str, Any]]) -> str:
    header, body = _table(result, manifest, columns, rows, totals)
    e = html.escape
    numeric_from = len(result["report"].get("group_by") or []) + 1
    head = "".join(f"<th{' class=n' if i >= numeric_from else ''}>{e(h)}</th>" for i, h in enumerate(header))
    lines = "".join(
        "<tr>" + "".join(f"<td{' class=n' if i >= numeric_from else ''}>{e(v)}</td>" for i, v in enumerate(line)) + "</tr>"
        for line in body)
    window = result["window"]
    period = f"{window['start']} to {window['end']}" if window["start"] else "All time"
    warnings = "".join(f"<li>{e(w)}</li>" for w in result["warnings"])
    facts = [("Period", period), ("Figures as of", result["evaluated_on"]),
             ("Recorded data through", result["data_through"] or "no data"), ("Lens", result["lens_note"]),
             ("Reporting currency", manifest["reporting_currency"]), ("Generation", result["generation"]),
             ("Exported", manifest["exported_at"]), ("App / schema", f"{manifest['app_version']} / {manifest['schema_version']}")]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(name)}</title>
<style>
:root{{color-scheme:light dark;--fg:#1d2522;--muted:#5d6b66;--line:#d9e0dd;--bg:#fff}}
@media(prefers-color-scheme:dark){{:root{{--fg:#e7ecea;--muted:#9aa8a3;--line:#33403b;--bg:#141a18}}}}
body{{font:14px/1.5 system-ui,sans-serif;color:var(--fg);background:var(--bg);margin:0;padding:24px 16px}}
main{{max-width:960px;margin:0 auto}} h1{{font-size:22px;margin:0 0 4px}}
dl{{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px;font-size:12px;color:var(--muted)}} dd{{margin:0}}
.scroll{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}}
.n{{text-align:right;font-variant-numeric:tabular-nums}} ul{{color:#a35b00}}
</style></head><body><main>
<h1>{e(name)}</h1>
<dl>{"".join(f"<dt>{e(k)}</dt><dd>{e(str(v))}</dd>" for k, v in facts)}</dl>
{f"<ul>{warnings}</ul>" if warnings else ""}
<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{lines}</tbody></table></div>
<script type="application/json" id="manifest">{json.dumps(manifest, ensure_ascii=False).replace("<", "\\u003c")}</script>
</main></body></html>
"""
