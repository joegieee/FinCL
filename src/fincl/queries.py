"""Snapshot-only dashboard queries. Decimal strings are authoritative; chart coordinates are not."""
from __future__ import annotations

import calendar
import json
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .common import category_strings
from .custom_categories import custom_categories
from .db import connect_readonly
from .decision_journal import default_journal_path, read_records
from .errors import FinCLError
from .generation import build_manifest, current_generation, generation_token
from .intervals import DateInterval, covers_date, month_span
from .report import _coverage_contract, _economic_flow


def money(value: Decimal) -> str:
    return format(value, 'f')


def all_entries(connection: Any) -> list[dict[str, Any]]:
    rows = connection.execute("""
      SELECT e.*, a.display_name AS account,
        coalesce(m.merchant_canonical, m.merchant_normalized, r.description_raw, d.description_raw, 'Unknown merchant') AS merchant,
        coalesce(r.description_raw, d.description_raw, '') AS description,
        coalesce(c.group_name || ' / ' || c.category, 'Unclassified / Unclassified') AS category,
        coalesce(c.basis, 'Not categorized') AS basis,
        coalesce('Page ' || d.page_number, 'Lines ' || r.physical_line_start || '–' || r.physical_line_end, 'Source record') AS source_location,
        coalesce(c.ambiguous, 0) AS ambiguous, coalesce(c.direction_conflict, 0) AS direction_conflict,
        i.classification,
        EXISTS(SELECT 1 FROM transfer_links t WHERE t.status='CONFIRMED' AND (t.left_entry_id=e.id OR t.right_entry_id=e.id)) AS linked,
        (e.analytics_eligible=1 AND e.lifecycle_state='COMPLETED' AND x.selection_state='SELECTED'
          AND (e.source_family='CSV' OR s.selection_state='SELECTED')) AS eligible
      FROM ledger_entries e JOIN accounts a ON a.id=e.account_id
      JOIN extraction_runs x ON x.id=e.extraction_run_id
      LEFT JOIN statements s ON s.id=e.statement_id
      LEFT JOIN raw_records r ON r.id=e.raw_record_id
      LEFT JOIN document_records d ON d.id=e.document_record_id
      LEFT JOIN merchant_observations m ON m.entry_id=e.id
      LEFT JOIN entry_categories c ON c.entry_id=e.id
      LEFT JOIN inbound_classifications i ON i.anchor=e.natural_anchor AND i.bound=1
      ORDER BY e.effective_at DESC, e.natural_anchor, e.id
    """).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        amount = abs(Decimal(item['amount_text']))
        bucket = 'excluded'
        if item['eligible']:
            if item['linked'] or item['classification'] == 'self_transfer':
                bucket = 'internal'
            elif item['economic_direction'] == 'OUT':
                bucket = 'spending'
            elif item['economic_direction'] == 'IN':
                bucket = {'income': 'income', 'reimbursement': 'reimbursements', 'gift': 'non_income_inflow', 'family_transfer': 'non_income_inflow', 'other': 'other'}.get(item['classification'], 'refunds' if item['classification'] is None and item['event_kind'] == 'REFUND' else 'pending_review')
        item.update(amount=money(-amount if item['economic_direction'] == 'OUT' else amount),
                    fee=money(abs(Decimal(item['fee_text'] or '0'))), currency=item['currency_pocket'],
                    date=item['effective_at'][:10], precision=item['effective_precision'], bucket=bucket,
                    anchor=item['natural_anchor'], review=bool(item['ambiguous'] or item['direction_conflict'] or item['category'].startswith('Unclassified') or bucket == 'pending_review'))
        result.append({key: item[key] for key in ('id','anchor','date','precision','amount','fee','currency','account','merchant','description','category','basis','source_location','bucket','eligible','review','source_family','lifecycle_state')})
    return result


def in_window(entry: dict[str, Any], start: str, end: str) -> bool:
    if entry['precision'] == 'MONTH':
        return DateInterval(start, end).contains_interval(month_span(entry['date'][:7]))
    return start <= entry['date'] <= end


def aggregate(entries: list[dict[str, Any]], key: str, buckets: set[str] | None = None) -> list[dict[str, Any]]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    counts: dict[str, int] = defaultdict(int)
    for item in entries:
        if item['bucket'] in (buckets or {'spending'}):
            name = item[key]
            totals[name] += abs(Decimal(item['amount']))
            counts[name] += 1
    return [{'name': key, 'value': money(value), 'count': counts[key]} for key, value in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))]


def transfer_proposals(connection: Any) -> list[dict[str, Any]]:
    """Automatic same-pair transfer links still awaiting a durable confirm/reject."""
    rows = connection.execute("""
      SELECT t.id, t.link_kind, t.method_version, t.evidence_json,
             t.left_currency, t.left_amount_text, t.right_currency, t.right_amount_text,
             le.natural_anchor AS left_anchor, re.natural_anchor AS right_anchor,
             le.effective_at AS left_at, re.effective_at AS right_at,
             la.display_name AS left_account, ra.display_name AS right_account,
             coalesce(lm.merchant_canonical, lm.merchant_normalized, lr.description_raw, ld.description_raw, '') AS left_description,
             coalesce(rm.merchant_canonical, rm.merchant_normalized, rr.description_raw, rd.description_raw, '') AS right_description
      FROM transfer_links t
      JOIN ledger_entries le ON le.id = t.left_entry_id
      JOIN ledger_entries re ON re.id = t.right_entry_id
      JOIN accounts la ON la.id = le.account_id
      JOIN accounts ra ON ra.id = re.account_id
      LEFT JOIN merchant_observations lm ON lm.entry_id = le.id
      LEFT JOIN merchant_observations rm ON rm.entry_id = re.id
      LEFT JOIN raw_records lr ON lr.id = le.raw_record_id
      LEFT JOIN raw_records rr ON rr.id = re.raw_record_id
      LEFT JOIN document_records ld ON ld.id = le.document_record_id
      LEFT JOIN document_records rd ON rd.id = re.document_record_id
      WHERE t.status = 'PROPOSED'
      ORDER BY t.created_at, t.id
    """).fetchall()
    result = []
    for row in rows:
        evidence = json.loads(row['evidence_json'])
        rejection = evidence.get('rejection') if isinstance(evidence.get('rejection'), dict) else None
        result.append({
            'id': row['id'], 'link_kind': row['link_kind'], 'method': row['method_version'],
            'left': row['left_anchor'], 'right': row['right_anchor'],
            'left_account': row['left_account'], 'right_account': row['right_account'],
            'left_description': row['left_description'], 'right_description': row['right_description'],
            'left_amount': money(abs(Decimal(row['left_amount_text']))), 'left_currency': row['left_currency'],
            'right_amount': money(abs(Decimal(row['right_amount_text']))), 'right_currency': row['right_currency'],
            'left_date': row['left_at'][:10], 'right_date': row['right_at'][:10],
            'basis': str(rejection['code']) if rejection else 'Matched by description and date across accounts',
        })
    return result


def dashboard(database: Path, *, start: str | None = None, end: str | None = None, currency: str = 'SGD', expected: str | None = None) -> dict[str, Any]:
    connection = connect_readonly(database)
    try:
        connection.execute('BEGIN')
        generation = current_generation(connection)
        if generation is None:
            raise FinCLError('Ledger is not ready. Run fincl refresh first.')
        token = generation['generation_token']
        if expected and expected != token:
            raise FinCLError('STALE_GENERATION: reload before continuing')
        manifest = json.loads(generation['manifest_json'])
        records = read_records(default_journal_path(database))
        stale = generation_token(build_manifest(connection)) != token or (records[-1].record_hash if records else '0'*64) != manifest['journal_head']
        entries = all_entries(connection)
        contract = _coverage_contract(connection)
        if (start is None) != (end is None):
            raise FinCLError('Choose both a start and end date')
        if start is None:
            # Latest full calendar month wholly covered; otherwise the latest covered interval.
            for interval in reversed(contract.covered_intervals):
                last = date.fromisoformat(interval.end)
                if last.day != calendar.monthrange(last.year, last.month)[1]:
                    last = last.replace(day=1) - timedelta(days=1)
                first = last.replace(day=1)
                if interval.start <= first.isoformat():
                    start, end = first.isoformat(), last.isoformat()
                    break
            if start is None and contract.covered_intervals:
                start, end = contract.covered_intervals[-1].start, contract.covered_intervals[-1].end
            if start is None:
                latest = max((e['date'] for e in entries), default=date.today().isoformat())
                start, end = latest[:7] + '-01', latest
        assert start is not None and end is not None
        try:
            date.fromisoformat(start)
            date.fromisoformat(end)
        except ValueError as exc:
            raise FinCLError('Dates must use YYYY-MM-DD') from exc
        if start > end:
            raise FinCLError('Start date must be before end date')
        covered = any(i.contains_interval(DateInterval(start, end)) for i in contract.covered_intervals)
        report = _economic_flow(connection, start=start, end=end) if covered else None
        bucket = next((b for b in report['currencies'] if b['currency'] == currency), None) if report else None
        summary = {key: (bucket[key] if bucket else '0') if covered else None for key in ('spending','income','fees','refunds','reimbursements','non_income_inflow','pending_review','net_economic_flow')}
        selected = [e for e in entries if e['currency'] == currency and in_window(e,start,end)]
        eligible = [e for e in selected if e['eligible']]
        # Withhold analytics entirely when the requested window crosses a coverage gap.
        analytical = eligible if covered else []
        groups = aggregate(analytical, 'category')
        merchants = aggregate(analytical, 'merchant')
        months: list[dict[str, Any]] = []
        # Six calendar months ending in selected month, with explicit nulls for gaps.
        end_date = date.fromisoformat(end)
        for offset in range(5, -1, -1):
            index = end_date.year * 12 + end_date.month - 1 - offset
            year, m = divmod(index, 12)
            m += 1
            month_first, month_last = date(year,m,1).isoformat(), date(year,m,calendar.monthrange(year,m)[1]).isoformat()
            full = any(i.contains_interval(DateInterval(month_first,month_last)) for i in contract.covered_intervals)
            values = _economic_flow(connection,start=month_first,end=month_last) if full else None
            b = next((b for b in values['currencies'] if b['currency']==currency),None) if values else None
            months.append({'month':month_first[:7], 'spending': money(Decimal(b['spending'])+Decimal(b['fees'])) if b else ('0' if full else None), 'income':b['income'] if b else ('0' if full else None), 'complete':full})
        recurring = []
        by_merchant: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for e in entries:
            if e['currency']==currency and e['bucket']=='spending' and e['precision']!='MONTH' and e['date'] <= end and covers_date(contract.covered_intervals,e['date']):
                by_merchant[e['merchant']].append(e)
        for merchant, items in by_merchant.items():
            chronological = sorted(items,key=lambda e:e['date'])
            if len(items) < 3:
                continue
            dates = [date.fromisoformat(e['date']) for e in chronological]
            gaps = [(b-a).days for a,b in zip(dates,dates[1:],strict=False)]
            cadence = 'Monthly' if all(25<=g<=35 for g in gaps[-3:]) else ('Weekly' if all(6<=g<=8 for g in gaps[-3:]) else None)
            amounts = [abs(Decimal(e['amount'])) for e in chronological[-4:]]
            if not cadence or max(amounts) > min(amounts)*Decimal('1.1'):
                continue
            recurring.append({'merchant':merchant,'cadence':cadence,'amount':money(amounts[-1]),'last_date':dates[-1].isoformat(), 'next_date':(dates[-1]+timedelta(days=30 if cadence=='Monthly' else 7)).isoformat(),'count':len(items),'status':'Candidate — verify with provider'})
        custom = custom_categories(database, int(manifest['journal_seq']))
        plans: dict[str, Any] = {}
        for record in records:
            if record.seq <= manifest['journal_seq'] and record.decision_kind == 'PLAN_SET':
                plans[record.anchors['plan']] = {'id':record.anchors['plan'],**record.payload}
        full_month = start.endswith('-01') and end==date(end_date.year,end_date.month,calendar.monthrange(end_date.year,end_date.month)[1]).isoformat() and start[:7]==end[:7]
        for plan in plans.values():
            relevant = [e for e in analytical if not plan['category'] or e['category']==plan['category']]
            spent = sum((abs(Decimal(e['amount'])) for e in relevant if e['bucket']=='spending'),Decimal(0))
            if not plan['category']:
                spent += sum((Decimal(e['fee']) for e in relevant),Decimal(0))
            plan['actual'] = money(spent) if plan['type']=='budget' and covered and full_month and plan['currency']==currency else (plan['saved'] if plan['type']=='goal' else None)
            plan['remaining'] = money(Decimal(plan['target'])-Decimal(plan['actual'])) if plan['actual'] is not None else None
        return {'generation':token,'freshness':'stale' if stale else 'current','updated_at':generation['completed_at'], 'start':start,'end':end,'currency':currency,'currencies':sorted({e['currency'] for e in entries} | {'SGD'}),'covered':covered,'coverage_reasons':contract.reasons,'summary':summary,'categories':sorted(category_strings()|custom),'custom_categories':sorted(custom),'category_totals':groups,'merchant_totals':merchants,'trend':months,'recurring':recurring,'plans':list(plans.values()),'entries':selected,'accounts':sorted({e['account'] for e in entries}), 'total_entries':len(entries),'transfer_proposals':transfer_proposals(connection)}
    finally:
        connection.close()
