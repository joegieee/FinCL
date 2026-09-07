"""Snapshot-only dashboard queries. Decimal strings are authoritative; chart coordinates are not."""
from __future__ import annotations

import base64
import binascii
import calendar
import json
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .analysis import handles
from .analysis.queries import analysis_page as analysis_page
from .common import category_strings
from .custom_categories import custom_categories
from .db import connect_readonly
from .decision_journal import default_journal_path, read_records
from .errors import FinCLError
from .generation import build_manifest, current_generation, generation_token
from .intervals import DateInterval, covers_date, month_span
from .money import format_decimal
from .report import _coverage_contract, _economic_flow

# One shared shaping of a ledger entry for every screen: the raw join, then the
# economic `bucket` and the `review` flag as SQL so a paged query can filter and
# sort on them before LIMIT. `dashboard()` and `transactions_page()` both read
# from this so they can never disagree on what a row's bucket is.
_ENTRY_VIEW = """
WITH base AS (
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
),
shaped AS (
  SELECT base.*,
    CASE
      WHEN NOT eligible THEN 'excluded'
      WHEN linked OR classification = 'self_transfer' THEN 'internal'
      WHEN economic_direction = 'OUT' THEN 'spending'
      WHEN economic_direction = 'IN' THEN CASE classification
        WHEN 'income' THEN 'income'
        WHEN 'reimbursement' THEN 'reimbursements'
        WHEN 'gift' THEN 'non_income_inflow'
        WHEN 'family_transfer' THEN 'non_income_inflow'
        WHEN 'other' THEN 'other'
        ELSE CASE WHEN classification IS NULL AND event_kind = 'REFUND' THEN 'refunds' ELSE 'pending_review' END
      END
      ELSE 'excluded'
    END AS bucket
  FROM base
)
SELECT shaped.*,
  (ambiguous OR direction_conflict OR category LIKE 'Unclassified%' OR bucket = 'pending_review') AS review,
  (coalesce(fee_coefficient, 0) <> 0) AS fee_nonzero,
  substr(effective_at, 1, 7) AS effective_month,
  CAST(amount_coefficient AS REAL) / power(10.0, amount_scale) AS amount_value
FROM shaped
"""

# Sort options exposed to the transaction list. Each is a deterministic ORDER BY
# with (natural_anchor, id) as the final tie-break, plus the keyset comparison
# that pages forward from a cursor row.
_SORTS: dict[str, dict[str, str]] = {
    "date": {  # newest first
        "order": "effective_at DESC, natural_anchor ASC, id ASC",
        "key": "effective_at",
        "after": "(effective_at < :k_val OR (effective_at = :k_val AND (natural_anchor > :k_anchor OR (natural_anchor = :k_anchor AND id > :k_id))))",
    },
    "amount": {  # largest outgoings first (most negative signed value first)
        "order": "amount_value ASC, natural_anchor ASC, id ASC",
        "key": "amount_text",
        "after": "(amount_value > CAST(:k_val AS REAL) OR (amount_value = CAST(:k_val AS REAL) AND (natural_anchor > :k_anchor OR (natural_anchor = :k_anchor AND id > :k_id))))",
    },
    "merchant": {  # A -> Z
        "order": "merchant ASC, natural_anchor ASC, id ASC",
        "key": "merchant",
        "after": "(merchant > :k_val OR (merchant = :k_val AND (natural_anchor > :k_anchor OR (natural_anchor = :k_anchor AND id > :k_id))))",
    },
}

_ROW_KEYS = ('id','anchor','date','precision','amount','fee','currency','account','merchant',
             'description','category','basis','source_location','bucket','eligible','review',
             'source_family','lifecycle_state')


def money(value: Decimal) -> str:
    return format(value, 'f')


def _shape(row: Any) -> dict[str, Any]:
    """Turn one `_ENTRY_VIEW` row into the screen dict; amounts stay exact strings."""
    item = dict(row)
    magnitude = abs(Decimal(item['amount_text']))
    item.update(
        amount=money(-magnitude if item['economic_direction'] == 'OUT' else magnitude),
        fee=money(abs(Decimal(item['fee_text'] or '0'))),
        currency=item['currency_pocket'],
        date=item['effective_at'][:10],
        precision=item['effective_precision'],
        anchor=item['natural_anchor'],
        review=bool(item['review']),
    )
    return {key: item[key] for key in _ROW_KEYS}


def all_entries(connection: Any) -> list[dict[str, Any]]:
    rows = connection.execute(
        f"{_ENTRY_VIEW} ORDER BY effective_at DESC, natural_anchor, id"
    ).fetchall()
    return [_shape(row) for row in rows]


def _decode_cursor(cursor: str | None) -> dict[str, str] | None:
    if not cursor:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if {'val', 'anchor', 'id'} <= set(payload) and all(isinstance(payload[k], str) for k in ('val', 'anchor', 'id')):
            return {'val': payload['val'], 'anchor': payload['anchor'], 'id': payload['id']}
    except (binascii.Error, ValueError, TypeError):
        pass
    raise FinCLError('The page cursor is not valid; reload the list')


def _encode_cursor(row: dict[str, Any], sort: str) -> str:
    payload = {'val': str(row[_SORTS[sort]['key']]), 'anchor': row['natural_anchor'], 'id': row['id']}
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode()


def _window_sql(param_start: str, param_end: str) -> str:
    """A MONTH-precision row counts only when its whole month is inside the window."""
    return (
        f"(CASE WHEN effective_precision = 'MONTH' "
        f"THEN date(effective_month || '-01') >= date({param_start}) "
        f"AND date(effective_month || '-01', '+1 month', '-1 day') <= date({param_end}) "
        f"ELSE substr(effective_at, 1, 10) BETWEEN {param_start} AND {param_end} END)"
    )


def _tx_filter(
    *, currency: str, start: str, end: str, include_excluded: bool,
    account: str, category: str, search: str, kind: str,
) -> tuple[str, dict[str, Any]]:
    """The shared WHERE clause + bound params for the transaction list and its
    bulk-selection anchor materialization, so both select exactly the same rows."""
    where = ['currency_pocket = :currency', _window_sql(':start', ':end')]
    params: dict[str, Any] = {'currency': currency, 'start': start, 'end': end}
    if not include_excluded:
        where.append('eligible')
    if account:
        where.append('account = :account')
        params['account'] = account
    if category:
        where.append('category = :category')
        params['category'] = category
    if search.strip():
        where.append("instr(lower(merchant || ' ' || description), lower(:search)) > 0")
        params['search'] = search.strip()
    if kind == 'fees':
        where.append('fee_nonzero')
    elif kind == 'review':
        where.append('review')
    elif kind:
        where.append('bucket = :kind')
        params['kind'] = kind
    return ' AND '.join(where), params


# Bounded materialization for "select all matching": a bulk correction beyond
# this needs a narrower filter. Chunked into <=200-item atomic batches by the UI.
BULK_SELECT_CAP = 1000


def transaction_anchors(
    database: Path, *, generation: str, start: str, end: str, currency: str = 'SGD',
    search: str = '', category: str = '', account: str = '', kind: str = '',
    include_excluded: bool = False,
) -> dict[str, Any]:
    """Every natural anchor matching the current transaction filter, pinned to
    ``generation`` and capped at ``BULK_SELECT_CAP``. The caller freezes this
    list; later filter changes do not change what a bulk commit touches."""
    connection = connect_readonly(database)
    try:
        connection.execute('BEGIN')
        current = current_generation(connection)
        if current is None:
            raise FinCLError('Ledger is not ready. Run fincl refresh first.')
        if generation != current['generation_token']:
            raise FinCLError('STALE_GENERATION: reload the list')
        for value in (start, end):
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise FinCLError('Dates must use YYYY-MM-DD') from exc
        clause, params = _tx_filter(
            currency=currency, start=start, end=end, include_excluded=include_excluded,
            account=account, category=category, search=search, kind=kind,
        )
        rows = connection.execute(
            f"SELECT natural_anchor FROM ({_ENTRY_VIEW}) WHERE {clause} "
            f"ORDER BY effective_at DESC, natural_anchor, id LIMIT :cap",
            {**params, 'cap': BULK_SELECT_CAP + 1},
        ).fetchall()
        anchors = [row['natural_anchor'] for row in rows[:BULK_SELECT_CAP]]
        return {
            'generation': generation,
            'anchors': anchors,
            'truncated': len(rows) > BULK_SELECT_CAP,
            'cap': BULK_SELECT_CAP,
        }
    finally:
        connection.close()


def transactions_page(
    database: Path, *, generation: str, start: str, end: str, currency: str = 'SGD',
    search: str = '', category: str = '', account: str = '', kind: str = '',
    include_excluded: bool = False, sort: str = 'date', cursor: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """One keyset-paged slice of the transaction list, pinned to `generation`."""
    if sort not in _SORTS:
        raise FinCLError('Unknown sort order')
    limit = max(1, min(int(limit), 200))
    key = _decode_cursor(cursor)
    connection = connect_readonly(database)
    try:
        connection.execute('BEGIN')
        current = current_generation(connection)
        if current is None:
            raise FinCLError('Ledger is not ready. Run fincl refresh first.')
        if generation != current['generation_token']:
            raise FinCLError('STALE_GENERATION: reload the list')
        for value in (start, end):
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise FinCLError('Dates must use YYYY-MM-DD') from exc

        clause, params = _tx_filter(
            currency=currency, start=start, end=end, include_excluded=include_excluded,
            account=account, category=category, search=search, kind=kind,
        )

        total = connection.execute(
            f"SELECT count(*) FROM ({_ENTRY_VIEW}) WHERE {clause}", params
        ).fetchone()[0]

        paged = clause
        if key is not None:
            paged = f"{clause} AND {_SORTS[sort]['after']}"
            params.update(k_val=key['val'], k_anchor=key['anchor'], k_id=key['id'])
        rows = connection.execute(
            f"SELECT * FROM ({_ENTRY_VIEW}) WHERE {paged} ORDER BY {_SORTS[sort]['order']} LIMIT :limit",
            {**params, 'limit': limit + 1},
        ).fetchall()

        has_more = len(rows) > limit
        window = [dict(row) for row in rows[:limit]]
        next_cursor = _encode_cursor(window[-1], sort) if has_more and window else None
        return {
            'generation': generation,
            'entries': [_shape(row) for row in window],
            'next_cursor': next_cursor,
            'total': int(total),
        }
    finally:
        connection.close()


_CONTRIB_KEYS = (
    'component_id', 'origin_component_id', 'entry_anchor', 'role', 'bucket',
    'value', 'currency', 'category', 'merchant', 'merchant_key', 'account',
    'date', 'precision', 'source_location', 'nets',
)


def _contrib_shape(row: Any) -> dict[str, Any]:
    item = dict(row)
    item.update(
        entry_anchor=item['origin_anchor'],
        value=format_decimal(int(item['signed_coefficient']), int(item['scale'])),
        currency=item['currency'],
        category=item['category_key'],
        merchant=item['merchant_display'],
        date=item['represented_start'],
        precision=item['date_precision'],
        nets=bool(item['nets']),
    )
    return {key: item[key] for key in _CONTRIB_KEYS}


def contributors_page(
    database: Path, *, handle: str, cursor: str | None = None, limit: int = 50,
) -> dict[str, Any]:
    """One keyset page of the exact components behind a displayed total.

    The ``handle`` is minted alongside a total or ranking row (see
    :func:`fincl.analysis.handles`); it binds the generation, lens, currency,
    window and bucket selection. A stale generation is refused, never answered
    against fresh data.
    """
    scope = handles.decode(handle)
    limit = max(1, min(int(limit), 200))
    key = _decode_cursor(cursor)
    connection = connect_readonly(database)
    try:
        connection.execute('BEGIN')
        current = current_generation(connection)
        if current is None:
            raise FinCLError('Ledger is not ready. Run fincl refresh first.')
        token = current['generation_token']
        if scope['generation'] != token:
            raise FinCLError('STALE_GENERATION: reopen Analysis')

        where = [
            'generation_token = :gen', 'lens = :lens', 'currency = :currency',
            'represented_start >= :start', 'represented_end <= :end',
        ]
        params: dict[str, Any] = {
            'gen': token, 'lens': scope['lens'], 'currency': scope['currency'],
            'start': scope['start'], 'end': scope['end'],
        }
        buckets = list(scope['buckets'])
        if buckets == ['net']:
            where.append('nets = 1')
        else:
            names = ', '.join(f':b{i}' for i in range(len(buckets)))
            where.append(f'bucket IN ({names})')
            params.update({f'b{i}': name for i, name in enumerate(buckets)})
        if scope.get('category_key') is not None:
            where.append('category_key = :category_key')
            params['category_key'] = scope['category_key']
        if scope.get('merchant_display') is not None:
            where.append('merchant_display = :merchant_display')
            params['merchant_display'] = scope['merchant_display']
        clause = ' AND '.join(where)

        magnitude = 'ABS(CAST(signed_coefficient AS REAL) / power(10.0, scale))'
        if scope['sort'] == 'amount':
            order = f'{magnitude} DESC, origin_anchor ASC, component_id ASC'
            after = (
                f'({magnitude} < CAST(:k_val AS REAL) OR ({magnitude} = CAST(:k_val AS REAL) '
                'AND (origin_anchor > :k_anchor OR (origin_anchor = :k_anchor AND component_id > :k_id))))'
            )
            key_sql = f'{magnitude}'
        else:
            order = 'represented_start DESC, origin_anchor ASC, component_id ASC'
            after = (
                '(represented_start < :k_val OR (represented_start = :k_val '
                'AND (origin_anchor > :k_anchor OR (origin_anchor = :k_anchor AND component_id > :k_id))))'
            )
            key_sql = 'represented_start'

        total = connection.execute(
            f'SELECT count(*) FROM analysis_components WHERE {clause}', params
        ).fetchone()[0]

        paged = clause
        if key is not None:
            paged = f'{clause} AND {after}'
            params.update(k_val=key['val'], k_anchor=key['anchor'], k_id=key['id'])
        rows = connection.execute(
            f'SELECT *, {key_sql} AS _k FROM analysis_components WHERE {paged} '
            f'ORDER BY {order} LIMIT :limit',
            {**params, 'limit': limit + 1},
        ).fetchall()

        has_more = len(rows) > limit
        window = [dict(row) for row in rows[:limit]]
        next_cursor = None
        if has_more and window:
            last = window[-1]
            payload = {
                'val': str(last['_k']),
                'anchor': last['origin_anchor'],
                'id': last['component_id'],
            }
            next_cursor = base64.urlsafe_b64encode(
                json.dumps(payload, separators=(',', ':')).encode()
            ).decode()
        return {
            'generation': token,
            'scope': {
                'lens': scope['lens'], 'currency': scope['currency'],
                'start': scope['start'], 'end': scope['end'],
                'buckets': buckets, 'sort': scope['sort'],
                'category': scope.get('category_key'),
                'merchant': scope.get('merchant_display'),
            },
            'components': [_contrib_shape(row) for row in window],
            'next_cursor': next_cursor,
            'total': int(total),
        }
    finally:
        connection.close()


def relationships_review(
    database: Path, *, kind: str = 'duplicate', generation: str | None = None,
    cursor: str | None = None, limit: int = 50,
) -> dict[str, Any]:
    """Automatic relationship proposals awaiting a durable decision.

    ``duplicate``: entry pairs that match on account, currency, exact amount,
    event kind, fee, date and normalized merchant across *distinct* evidence
    blobs, and are not already confirmed, rejected or suppressed. Similarity
    alone never suppresses a row — this is a review queue, not an action.
    """
    if kind != 'duplicate':
        raise FinCLError('Only duplicate review is available')
    limit = max(1, min(int(limit), 200))
    offset = 0
    if cursor:
        try:
            offset = max(0, int(base64.urlsafe_b64decode(cursor.encode()).decode()))
        except (binascii.Error, ValueError):
            raise FinCLError('The review cursor is not valid; reload the queue') from None
    connection = connect_readonly(database)
    try:
        connection.execute('BEGIN')
        current = current_generation(connection)
        if current is None:
            raise FinCLError('Ledger is not ready. Run fincl refresh first.')
        token = current['generation_token']
        if generation and generation != token:
            raise FinCLError('STALE_GENERATION: reload the queue')
        rows = connection.execute("""
          WITH shaped AS (
            SELECT e.id, e.natural_anchor AS anchor, e.evidence_sha256,
                   e.account_id, e.currency_pocket AS currency,
                   e.amount_text, e.amount_coefficient, e.amount_scale,
                   e.event_kind, COALESCE(e.fee_coefficient, 0) AS fee_coefficient,
                   substr(e.effective_at, 1, 10) AS posted_date,
                   COALESCE(mo.merchant_canonical, mo.merchant_normalized, r.description_raw,
                            d.description_raw, '') AS merchant,
                   a.display_name AS account
            FROM ledger_entries e
            JOIN extraction_runs x ON x.id = e.extraction_run_id AND x.selection_state = 'SELECTED'
            JOIN accounts a ON a.id = e.account_id
            LEFT JOIN merchant_observations mo ON mo.entry_id = e.id
            LEFT JOIN raw_records r ON r.id = e.raw_record_id
            LEFT JOIN document_records d ON d.id = e.document_record_id
            WHERE e.analytics_eligible = 1 AND e.lifecycle_state = 'COMPLETED'
          )
          SELECT * FROM shaped WHERE (account_id, currency, amount_coefficient, amount_scale,
                 event_kind, fee_coefficient, posted_date, merchant) IN (
            SELECT account_id, currency, amount_coefficient, amount_scale, event_kind,
                   fee_coefficient, posted_date, merchant
            FROM shaped GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
            HAVING count(*) > 1 AND count(DISTINCT evidence_sha256) > 1
          )
          ORDER BY posted_date, amount_coefficient, anchor
        """).fetchall()
        decided = {
            row[0] for row in connection.execute(
                "SELECT identity FROM relationship_decisions WHERE kind = 'DUPLICATE'"
            )
        }
        suppressed = {
            row[0] for row in connection.execute(
                "SELECT suppressed_anchor FROM duplicate_suppressions"
            )
        }
        groups: dict[tuple[Any, ...], list[Any]] = defaultdict(list)
        for row in rows:
            groups[(
                row['account_id'], row['currency'], row['amount_coefficient'],
                row['amount_scale'], row['event_kind'], row['fee_coefficient'],
                row['posted_date'], row['merchant'],
            )].append(row)
        proposals: list[dict[str, Any]] = []
        for members in groups.values():
            members = sorted(members, key=lambda r: r['anchor'])
            for first, second in zip(members, members[1:], strict=False):
                identity = '\x1f'.join(sorted((first['anchor'], second['anchor'])))
                if identity in decided or first['anchor'] in suppressed or second['anchor'] in suppressed:
                    continue
                proposals.append({
                    'primary': first['anchor'], 'candidate': second['anchor'],
                    'account': first['account'], 'currency': first['currency'],
                    'amount': money(abs(Decimal(first['amount_text']))),
                    'date': first['posted_date'], 'merchant': first['merchant'] or 'Unknown merchant',
                    'event_kind': first['event_kind'],
                    'basis': 'Same account, amount, date and merchant across two imports',
                })
        window = proposals[offset:offset + limit]
        next_cursor = (
            base64.urlsafe_b64encode(str(offset + limit).encode()).decode()
            if offset + limit < len(proposals) else None
        )
        return {
            'generation': token, 'kind': 'duplicate', 'proposals': window,
            'next_cursor': next_cursor, 'total': len(proposals),
        }
    finally:
        connection.close()


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
        # The full list is served page-by-page from /transactions; the dashboard
        # payload only carries what Overview draws directly.
        recent = [e for e in selected if e['bucket'] == 'spending'][:8]
        review_count = sum(1 for e in selected if e['review'] and e['eligible'])
        # Withhold analytics entirely when the requested window crosses a coverage gap.
        analytical = eligible if covered else []
        groups = aggregate(analytical, 'category')
        merchants = aggregate(analytical, 'merchant')

        # A contributor handle names the exact components behind each total. It is
        # bound to this generation and window; a later request against a changed
        # generation is refused. Only meaningful when the window is covered.
        def _handle(buckets: list[str], **narrow: str) -> str | None:
            if not covered:
                return None
            return handles.encode(
                generation=token, lens='recorded_flow', currency=currency,
                start=start, end=end, buckets=buckets, sort='amount', **narrow,
            )

        for row in groups:
            row['handle'] = _handle(['spending'], category_key=row['name'])
        for row in merchants:
            row['handle'] = _handle(['spending'], merchant_display=row['name'])
        summary_handles = {
            key: _handle(['net'] if key == 'net_economic_flow' else [key])
            for key in summary
        }
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
        return {'generation':token,'freshness':'stale' if stale else 'current','updated_at':generation['completed_at'], 'start':start,'end':end,'currency':currency,'currencies':sorted({e['currency'] for e in entries} | {'SGD'}),'covered':covered,'coverage_reasons':contract.reasons,'summary':summary,'summary_handles':summary_handles,'categories':sorted(category_strings()|custom),'custom_categories':sorted(custom),'category_totals':groups,'merchant_totals':merchants,'trend':months,'recurring':recurring,'plans':list(plans.values()),'recent':recent,'review_count':review_count,'accounts':sorted({e['account'] for e in entries}), 'total_entries':len(entries),'transfer_proposals':transfer_proposals(connection)}
    finally:
        connection.close()
