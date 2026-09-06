"""Authenticated loopback HTTP application. Financial state lives in the ledger/journal."""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .application import record_batch, record_decision
from .common import canonical_json, resolve_category
from .custom_categories import custom_categories, validate_creation
from .db import connect_readonly
from .decision_journal import default_journal_path, read_records
from .decisions import resolve_anchor
from .errors import FinCLError
from .generation import build_manifest, current_generation, generation_token
from .locking import LedgerBusyError, ledger_writer
from .plans import validate_plan
from .queries import analysis_page, contributors_page, dashboard, transactions_page
from .refresh import refresh_ledger


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


_SINGLE_KINDS = Literal[
    'CATEGORY_OVERRIDE','CATEGORY_CREATE','PLAN_SET','INBOUND_CLASSIFICATION',
    'TRANSFER_DECISION','DUPLICATE_CONFIRM','DUPLICATE_REJECT','DUPLICATE_RETIRE',
    'CATEGORY_OVERRIDE_RETIRE','REFUND_ALLOCATION_SET','REFUND_ALLOCATION_RETIRE',
    'REFUND_LINK_REJECT',
    # v0.5 setup / accounts / categories / rules / review kinds
    'CATEGORY_DETAILS_SET','CATEGORY_ARCHIVE_SET','ACCOUNT_SCOPE_SET',
    'ACCOUNT_DETAILS_SET','ACCOUNT_ALIAS_SET','ACCOUNT_ALIAS_RETIRE',
    'REVIEW_DEFER_SET','REVIEW_DEFER_RETIRE','MERCHANT_ALIAS_SET',
    'MERCHANT_ALIAS_RETIRE','CATEGORY_RULE_SET','CATEGORY_RULE_RETIRE',
    # v0.3 personal debts and shared expenses
    'PERSONAL_DEBT_OPEN','PERSONAL_DEBT_DETAILS_SET','PERSONAL_DEBT_EVENT_SET',
    'PERSONAL_DEBT_EVENT_RETIRE','PERSONAL_DEBT_CLOSE','PERSONAL_DEBT_REOPEN',
    'ENTRY_RESIDUAL_SET','ENTRY_RESIDUAL_RETIRE','INBOUND_CLASSIFICATION_RETIRE',
    # v0.4 credit-card facilities, dated limit observations, manual bill allocation
    'CARD_FACILITY_SET','CARD_FACILITY_RETIRE','CARD_LIMIT_OBSERVATION_SET',
    'CARD_LIMIT_OBSERVATION_RETIRE','CARD_PAYMENT_ALLOCATION_SET',
    'CARD_PAYMENT_ALLOCATION_RETIRE',
    # v0.4 installment plans, purchase allocations and early settlement
    'INSTALLMENT_PLAN_SET','INSTALLMENT_PLAN_RETIRE','INSTALLMENT_ALLOCATION_SET',
    'INSTALLMENT_ALLOCATION_RETIRE','INSTALLMENT_SETTLEMENT_SET',
    'INSTALLMENT_SETTLEMENT_RETIRE',
]

_DEBT_KINDS = frozenset({
    'PERSONAL_DEBT_OPEN','PERSONAL_DEBT_DETAILS_SET','PERSONAL_DEBT_EVENT_SET',
    'PERSONAL_DEBT_EVENT_RETIRE','PERSONAL_DEBT_CLOSE','PERSONAL_DEBT_REOPEN',
    'ENTRY_RESIDUAL_SET','ENTRY_RESIDUAL_RETIRE','INBOUND_CLASSIFICATION_RETIRE',
})

_CARD_KINDS = frozenset({
    'CARD_FACILITY_SET','CARD_FACILITY_RETIRE','CARD_LIMIT_OBSERVATION_SET',
    'CARD_LIMIT_OBSERVATION_RETIRE','CARD_PAYMENT_ALLOCATION_SET',
    'CARD_PAYMENT_ALLOCATION_RETIRE',
})

_INSTALLMENT_KINDS = frozenset({
    'INSTALLMENT_PLAN_SET','INSTALLMENT_PLAN_RETIRE','INSTALLMENT_ALLOCATION_SET',
    'INSTALLMENT_ALLOCATION_RETIRE','INSTALLMENT_SETTLEMENT_SET',
    'INSTALLMENT_SETTLEMENT_RETIRE',
})


class Command(StrictModel):
    request_id: UUID
    expected_generation: str = Field(min_length=1,max_length=100)
    kind: _SINGLE_KINDS
    anchors: dict[str,str]
    payload: dict[str,Any]


class BatchItem(StrictModel):
    kind: Literal[
        'CATEGORY_OVERRIDE','CATEGORY_OVERRIDE_RETIRE','INBOUND_CLASSIFICATION',
        'DUPLICATE_CONFIRM','DUPLICATE_REJECT','DUPLICATE_RETIRE',
    ]
    anchors: dict[str,str]
    payload: dict[str,str]


class Batch(StrictModel):
    request_id: UUID
    expected_generation: str = Field(min_length=1,max_length=100)
    items: list[BatchItem] = Field(min_length=1,max_length=200)
    reason: str = Field(min_length=1,max_length=2000)


class Bootstrap(StrictModel):
    secret: str = Field(max_length=256)


class Entry(StrictModel):
    id: str
    anchor: str
    date: str
    precision: str
    amount: str
    fee: str
    currency: str
    account: str
    merchant: str
    description: str
    category: str
    basis: str
    source_location: str
    bucket: str
    eligible: bool
    review: bool
    source_family: str
    lifecycle_state: str


class Total(StrictModel):
    name: str
    value: str
    count: int
    handle: str | None = None


class Proposal(StrictModel):
    id: str
    link_kind: str
    method: str
    left: str
    right: str
    left_account: str
    right_account: str
    left_description: str
    right_description: str
    left_amount: str
    left_currency: str
    right_amount: str
    right_currency: str
    left_date: str
    right_date: str
    basis: str


class Trend(StrictModel):
    month: str
    spending: str | None
    income: str | None
    complete: bool


class Recurring(StrictModel):
    merchant: str
    cadence: str
    amount: str
    last_date: str
    next_date: str
    count: int
    status: str


class Plan(StrictModel):
    id: str
    type: str
    name: str
    currency: str
    target: str
    saved: str
    category: str
    reason: str
    actual: str | None
    remaining: str | None


class Dashboard(StrictModel):
    generation: str
    freshness: str
    updated_at: str
    start: str
    end: str
    currency: str
    currencies: list[str]
    covered: bool
    coverage_reasons: list[str]
    summary: dict[str,str | None]
    summary_handles: dict[str,str | None]
    categories: list[str]
    custom_categories: list[str]
    category_totals: list[Total]
    merchant_totals: list[Total]
    trend: list[Trend]
    recurring: list[Recurring]
    plans: list[Plan]
    recent: list[Entry]
    review_count: int
    accounts: list[str]
    total_entries: int
    transfer_proposals: list[Proposal]


class TransactionsPage(StrictModel):
    generation: str
    entries: list[Entry]
    next_cursor: str | None
    total: int


class Contributor(StrictModel):
    component_id: str
    origin_component_id: str
    entry_anchor: str
    role: str
    bucket: str
    value: str
    currency: str
    category: str
    merchant: str
    merchant_key: str | None
    account: str
    date: str
    precision: str
    source_location: str
    nets: bool


class ContributorScope(StrictModel):
    lens: str
    currency: str
    start: str
    end: str
    buckets: list[str]
    sort: str
    category: str | None
    merchant: str | None


class ContributorsPage(StrictModel):
    generation: str
    scope: ContributorScope
    components: list[Contributor]
    next_cursor: str | None
    total: int


class MoneyDTO(StrictModel):
    value: str
    currency: str


class SummaryCell(StrictModel):
    value: MoneyDTO | None
    previous: MoneyDTO | None
    delta: MoneyDTO | None
    pct: str | None


class AnalysisScopeDTO(StrictModel):
    start: str
    end: str
    currency: str
    lens: str
    accounts: list[str]
    category_keys: list[str]
    merchant: str | None


class ComparisonDTO(StrictModel):
    kind: str
    start: str | None
    end: str | None
    available: bool
    reasons: list[str]


class TrendPoint(StrictModel):
    month: str
    spending: MoneyDTO | None
    fees: MoneyDTO | None
    income: MoneyDTO | None
    complete: bool


class CategoryChange(StrictModel):
    category: str
    current: MoneyDTO
    previous: MoneyDTO
    delta: MoneyDTO
    pct: str | None
    new_in_period: bool
    handle: str | None


class CategoryChanges(StrictModel):
    increases: list[CategoryChange]
    decreases: list[CategoryChange]
    restated_using_current_categories: bool


class MerchantRank(StrictModel):
    merchant: str
    value: MoneyDTO
    count: int
    handle: str | None


class CategoryCost(StrictModel):
    category: str
    gross: MoneyDTO
    refunds_received: MoneyDTO
    net_cost: MoneyDTO


class ReviewProposal(StrictModel):
    primary: str
    candidate: str
    account: str
    currency: str
    amount: str
    date: str
    merchant: str
    event_kind: str
    basis: str


class RelationshipsReview(StrictModel):
    generation: str
    kind: str
    proposals: list[ReviewProposal]
    next_cursor: str | None
    total: int


class Analysis(StrictModel):
    generation: str
    freshness: str
    updated_at: str
    scope: AnalysisScopeDTO
    covered: bool
    coverage_reasons: list[str]
    comparison: ComparisonDTO
    summary: dict[str, SummaryCell]
    summary_handles: dict[str, str | None]
    trend: list[TrendPoint]
    category_changes: CategoryChanges
    merchant_ranking: list[MerchantRank]
    receipt_period_cost: list[CategoryCost]
    contributors_handle: str | None


def _validate(database: Path, command: Command) -> dict[str,str]:
    """Raise on an invalid command; return journal-side values derived during validation."""
    extra: dict[str,str] = {}
    connection = connect_readonly(database)
    try:
        generation = current_generation(connection)
        if generation is None or generation['generation_token'] != command.expected_generation:
            raise HTTPException(409,'STALE_GENERATION: reload and preview again. Your draft is preserved.')
        records = read_records(default_journal_path(database))
        manifest = json.loads(generation['manifest_json'])
        if generation_token(build_manifest(connection)) != generation['generation_token'] or (records[-1].record_hash if records else '0'*64) != manifest['journal_head']:
            raise HTTPException(409, 'STALE_GENERATION: refresh before editing.')
        if not command.payload.get('reason','').strip() or len(command.payload['reason']) > 2000:
            raise FinCLError('A reason of 1–2,000 characters is required')
        custom = custom_categories(database)
        if command.kind == 'CATEGORY_CREATE':
            if set(command.payload) != {'category','reason'}:
                raise FinCLError('Invalid category fields')
            validate_creation(command.anchors,command.payload,custom)
        elif command.kind == 'PLAN_SET':
            validate_plan(command.anchors,command.payload)
            if command.payload['category']:
                resolve_category(command.payload['category'],custom)
        elif command.kind == 'TRANSFER_DECISION':
            if set(command.anchors) != {'left','right'}:
                raise FinCLError('A transfer decision needs a left and right transaction')
            if set(command.payload) != {'decision','reason'} or command.payload['decision'] not in {'confirm','reject'}:
                raise FinCLError('Choose confirm or reject')
            row = connection.execute(
                "SELECT t.link_kind, t.method_version FROM transfer_links t "
                "JOIN ledger_entries le ON le.id=t.left_entry_id "
                "JOIN ledger_entries re ON re.id=t.right_entry_id "
                "WHERE t.status='PROPOSED' AND le.natural_anchor=? AND re.natural_anchor=?",
                (command.anchors['left'],command.anchors['right'])).fetchone()
            if row is None:
                raise FinCLError('This transfer proposal is no longer open')
            extra = {'link_kind':row['link_kind'],'policy_version':row['method_version']}
        elif command.kind in {'DUPLICATE_CONFIRM','DUPLICATE_REJECT','DUPLICATE_RETIRE'}:
            from .relationships import validate_duplicate
            validate_duplicate(command.kind, command.anchors, command.payload)
            for role in ('primary','candidate'):
                if resolve_anchor(connection, command.anchors[role]) is None:
                    raise FinCLError(f'The {role} transaction no longer resolves')
        elif command.kind == 'CATEGORY_OVERRIDE_RETIRE':
            from .relationships import validate_override_retire
            validate_override_retire(command.anchors, command.payload)
            if resolve_anchor(connection, command.anchors['entry']) is None:
                raise FinCLError('The transaction no longer resolves')
        elif command.kind in {'REFUND_ALLOCATION_SET','REFUND_ALLOCATION_RETIRE','REFUND_LINK_REJECT'}:
            from .relationships import preview_refund_allocation, validate_refund_allocation
            validate_refund_allocation(command.kind, command.anchors, command.payload)
            for anchor in command.anchors.values():
                if resolve_anchor(connection, anchor) is None:
                    raise FinCLError('A referenced transaction no longer resolves')
            if command.kind == 'REFUND_ALLOCATION_SET':
                totals = preview_refund_allocation(
                    connection, command.anchors['refund'],
                    list(command.payload['allocations']),
                    resolve_anchor=lambda a: resolve_anchor(connection, a),
                )
                extra = {k: str(v) for k, v in totals.items()}
        elif command.kind in {
            'CATEGORY_DETAILS_SET','CATEGORY_ARCHIVE_SET','ACCOUNT_SCOPE_SET',
            'ACCOUNT_DETAILS_SET','ACCOUNT_ALIAS_SET','ACCOUNT_ALIAS_RETIRE',
            'REVIEW_DEFER_SET','REVIEW_DEFER_RETIRE','MERCHANT_ALIAS_SET',
            'MERCHANT_ALIAS_RETIRE','CATEGORY_RULE_SET','CATEGORY_RULE_RETIRE',
        }:
            from .v05 import validate_v05
            validate_v05(command.kind, command.anchors, command.payload, database=database)
            if command.kind.startswith('ACCOUNT_'):
                account_id = command.anchors.get('account') or str(command.payload.get('account_id',''))
                if account_id and not connection.execute('SELECT 1 FROM accounts WHERE id=?',(account_id,)).fetchone():
                    raise FinCLError('The selected account no longer exists')
        elif command.kind in _DEBT_KINDS:
            from .db import connect_sqlite, initialize_schema
            from .debts.preview import preview_debt
            from .decisions import replay_journal
            # The full-state check needs a writable connection for its SAVEPOINT
            # dry-run; the caller already holds the ledger writer lock.
            preview_connection = connect_sqlite(database)
            try:
                initialize_schema(preview_connection)
                replay_journal(preview_connection)
                preview_debt(
                    preview_connection, command.kind, dict(command.anchors),
                    dict(command.payload),
                    resolve_anchor=lambda a: resolve_anchor(preview_connection, a),
                )
            finally:
                preview_connection.close()
        elif command.kind in _CARD_KINDS or command.kind in _INSTALLMENT_KINDS:
            from .db import connect_sqlite, initialize_schema
            from .decisions import replay_journal
            preview_connection = connect_sqlite(database)
            try:
                initialize_schema(preview_connection)
                replay_journal(preview_connection)
                if command.kind in _CARD_KINDS:
                    from .cards.preview import preview_card
                    preview_card(
                        preview_connection, command.kind, dict(command.anchors),
                        dict(command.payload),
                        resolve_anchor=lambda a: resolve_anchor(preview_connection, a),
                    )
                else:
                    from .installments.preview import preview_installment
                    preview_installment(
                        preview_connection, command.kind, dict(command.anchors),
                        dict(command.payload),
                        resolve_anchor=lambda a: resolve_anchor(preview_connection, a),
                    )
            finally:
                preview_connection.close()
        else:
            if set(command.anchors) != {'entry'} or not connection.execute('SELECT 1 FROM ledger_entries WHERE natural_anchor=?',(command.anchors['entry'],)).fetchone():
                raise FinCLError('The transaction no longer resolves')
            if command.kind == 'CATEGORY_OVERRIDE':
                if set(command.payload) != {'category','reason'}:
                    raise FinCLError('Invalid category fields')
                group, _, _ = resolve_category(command.payload['category'],custom)
                if group=='Internal':
                    raise FinCLError('Internal categories are assigned by transfer decisions')
            else:
                if set(command.payload) != {'classification','reason'} or command.payload['classification'] not in {'income','reimbursement','gift','family_transfer','self_transfer','other'}:
                    raise FinCLError('Choose a supported inflow classification')
                if not connection.execute("SELECT 1 FROM ledger_entries WHERE natural_anchor=? AND economic_direction='IN'",(command.anchors['entry'],)).fetchone():
                    raise FinCLError('Only an inbound transaction can be classified')
        return extra
    finally:
        connection.close()


def create_app(database: Path, *, port: int = 8765, bootstrap_secret: str | None = None, assets: Path | None = None, evidence_dir: Path | None = None, demo: bool = False) -> FastAPI:
    database = database.resolve()
    app = FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
    app.state.bootstrap_secret = bootstrap_secret or secrets.token_urlsafe(32)
    app.state.session = secrets.token_urlsafe(32)
    app.state.csrf = secrets.token_urlsafe(32)
    app.state.bootstrapped = False
    origin = f'http://127.0.0.1:{port}'
    cookie_name = f'fincl_session_{port}'
    assets = assets or Path(__file__).parent / 'static'

    @app.middleware('http')
    async def boundary(request: Request, call_next: Any) -> Any:
        if request.headers.get('host') != f'127.0.0.1:{port}':
            return JSONResponse({'detail':'Invalid host'},status_code=403)
        if request.headers.get('origin') not in (None,origin):
            return JSONResponse({'detail':'Invalid origin'},status_code=403)
        content_type = request.headers.get('content-type','').split(';')[0]
        allowed_write_type = content_type == 'application/json' or (
            request.url.path == '/api/v1/imports' and content_type == 'multipart/form-data'
        )
        if request.method not in ('GET','HEAD') and (request.headers.get('origin') != origin or not allowed_write_type):
            return JSONResponse({'detail':'Same-origin JSON is required'},status_code=403)
        if request.url.path.startswith('/api/'):
            if not app.state.bootstrapped or not secrets.compare_digest(request.cookies.get(cookie_name,''),app.state.session):
                return JSONResponse({'detail':'Session expired. Reopen the link from fincl serve.'},status_code=401)
            if request.method != 'GET' and not secrets.compare_digest(request.headers.get('x-csrf-token',''),app.state.csrf):
                return JSONResponse({'detail':'Invalid CSRF token'},status_code=403)
        response = await call_next(request)
        response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"})
        return response

    @app.exception_handler(FinCLError)
    async def domain_error(request: Request, exc: FinCLError) -> JSONResponse:
        return JSONResponse({'detail':str(exc),'recorded':False},status_code=409 if isinstance(exc,LedgerBusyError) else 422)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({'detail':'The operation could not complete. Retry with the same request or inspect the local server.','recorded':None},status_code=500)

    @app.post('/session')
    def session(body: Bootstrap) -> JSONResponse:
        if app.state.bootstrapped or not secrets.compare_digest(body.secret,app.state.bootstrap_secret):
            raise HTTPException(403,'Launch link already used or invalid. Relaunch fincl serve.')
        app.state.bootstrapped = True
        response = JSONResponse({'csrf':app.state.csrf})
        response.set_cookie(cookie_name,app.state.session,httponly=True,samesite='strict')
        return response

    @app.get('/api/v1/health')
    def health() -> dict[str,Any]:
        return {'csrf':app.state.csrf,'demo':demo,'label':'Demo ledger' if demo else 'Personal ledger'}

    # --- v0.5: guided setup, in-app imports, accounts, categories, rules, review ---

    @app.get('/api/v1/setup/options')
    def setup_read() -> Any:
        from .v05 import setup_options
        return setup_options(database)

    @app.get('/api/v1/imports')
    def imports_read() -> Any:
        from .v05 import imports_page
        return imports_page(database)

    @app.get('/api/v1/imports/{import_id}')
    def import_read(import_id: str) -> Any:
        from .v05 import import_detail
        return import_detail(database, import_id)

    @app.post('/api/v1/imports')
    def imports_create(
        files: list[UploadFile] = File(...),
        adapter: str = Form(...),
    ) -> Any:
        from .intake import MAX_FILES, import_stream
        if not files or len(files) > MAX_FILES:
            raise FinCLError(f'Choose between 1 and {MAX_FILES} files')
        results: list[dict[str,object]] = []
        with ledger_writer(database) as writer:
            for upload in files:
                try:
                    results.append(import_stream(
                        database,
                        evidence_dir or database.parent / 'evidence',
                        stream=upload.file,
                        filename=upload.filename or 'upload',
                        adapter=adapter,
                        writer=writer,
                    ))
                except Exception as exc:
                    results.append({
                        'filename':Path(upload.filename or 'upload').name,
                        'adapter':adapter,
                        'status':'FAILED',
                        'error':str(exc),
                    })
                finally:
                    upload.file.close()
        return {'results':results,'accepted':sum(r.get('status') not in {'FAILED'} for r in results)}

    @app.get('/api/v1/accounts')
    def accounts_read() -> Any:
        from .v05 import accounts_page
        return accounts_page(database)

    @app.get('/api/v1/accounts/{account_id}/coverage')
    def account_coverage(account_id: str) -> Any:
        from .v05 import accounts_page
        page = accounts_page(database)
        account = next((row for row in page['accounts'] if row['id'] == account_id), None)
        if account is None:
            raise HTTPException(404,'Account not found')
        return {'generation':page['generation'],'account':account}

    @app.get('/api/v1/categories')
    def categories_read() -> Any:
        from .v05 import categories_page
        return categories_page(database)

    @app.get('/api/v1/rules')
    def rules_read() -> Any:
        from .v05 import rules_page
        return rules_page(database)

    @app.post('/api/v1/rules/preview')
    def rules_preview(command: Command) -> Any:
        if command.kind not in {'CATEGORY_RULE_SET','CATEGORY_RULE_RETIRE','MERCHANT_ALIAS_SET','MERCHANT_ALIAS_RETIRE'}:
            raise FinCLError('Choose a rule or merchant-alias command')
        with ledger_writer(database):
            _validate(database,command)
        return {'valid':True,'generation':command.expected_generation,'effect':command.payload}

    @app.get('/api/v1/review')
    def unified_review() -> Any:
        from .v05 import review_page
        return review_page(database)

    @app.get('/api/v1/history/{sequence}/inverse-preview')
    def history_inverse(sequence: int) -> Any:
        from .v05 import inverse_preview
        return inverse_preview(database, sequence)

    @app.get('/api/v1/overview',response_model=Dashboard)
    def overview(start: str | None = None,end: str | None = None,currency: str = 'SGD') -> Any:
        with ledger_writer(database):
            return dashboard(database,start=start,end=end,currency=currency)

    @app.get('/api/v1/transactions',response_model=TransactionsPage)
    def transactions(generation: str, start: str, end: str, currency: str = 'SGD',
                     search: str = '', category: str = '', account: str = '', kind: str = '',
                     include_excluded: bool = False, sort: str = 'date',
                     cursor: str | None = None, limit: int = 50) -> Any:
        with ledger_writer(database):
            try:
                return transactions_page(
                    database, generation=generation, start=start, end=end, currency=currency,
                    search=search, category=category, account=account, kind=kind,
                    include_excluded=include_excluded, sort=sort, cursor=cursor, limit=limit,
                )
            except FinCLError as exc:
                if str(exc).startswith('STALE_GENERATION'):
                    raise HTTPException(409, str(exc)) from exc
                raise

    @app.get('/api/v1/analysis',response_model=Analysis)
    def analysis(
        start: str, end: str, currency: str = 'SGD',
        comparison: str | None = None,
        accounts: list[str] | None = Query(default=None),
        category_keys: list[str] | None = Query(default=None),
        merchant: str | None = None,
        expected_generation: str | None = None,
    ) -> Any:
        with ledger_writer(database):
            try:
                return analysis_page(
                    database, start=start, end=end, currency=currency,
                    accounts=accounts, category_keys=category_keys, merchant=merchant,
                    comparison=comparison, expected=expected_generation,
                )
            except FinCLError as exc:
                if str(exc).startswith('STALE_GENERATION'):
                    raise HTTPException(409, str(exc)) from exc
                raise

    @app.get('/api/v1/contributors/{handle}',response_model=ContributorsPage)
    def contributors(handle: str, cursor: str | None = None, limit: int = 50) -> Any:
        with ledger_writer(database):
            try:
                return contributors_page(database, handle=handle, cursor=cursor, limit=limit)
            except FinCLError as exc:
                if str(exc).startswith('STALE_GENERATION'):
                    raise HTTPException(409, str(exc)) from exc
                raise

    @app.get('/api/v1/debts')
    def debts(as_of: str | None = None, currency: str | None = None,
              status: str | None = None, cursor: str | None = None,
              limit: int = 50, generation: str | None = None) -> Any:
        from .debts.queries import debts_page
        with ledger_writer(database):
            try:
                return debts_page(database, as_of=as_of, currency=currency,
                                  status=status, cursor=cursor, limit=limit,
                                  expected=generation)
            except FinCLError as exc:
                if str(exc).startswith('STALE_GENERATION'):
                    raise HTTPException(409, str(exc)) from exc
                raise HTTPException(422, str(exc)) from exc

    @app.get('/api/v1/debts/allocation-candidates')
    def debt_allocation_candidates(debt: str, event_kind: str | None = None,
                                   cursor: str | None = None, limit: int = 50,
                                   generation: str | None = None) -> Any:
        from .debts.queries import allocation_candidates
        with ledger_writer(database):
            try:
                return allocation_candidates(database, debt_id=debt, event_kind=event_kind,
                                             cursor=cursor, limit=limit, expected=generation)
            except FinCLError as exc:
                if str(exc).startswith('STALE_GENERATION'):
                    raise HTTPException(409, str(exc)) from exc
                raise HTTPException(404 if 'does not exist' in str(exc) else 422, str(exc)) from exc

    @app.get('/api/v1/debts/{debt_id}')
    def debt(debt_id: str, as_of: str | None = None, generation: str | None = None) -> Any:
        from .debts.queries import debt_detail
        with ledger_writer(database):
            try:
                return debt_detail(database, debt_id, as_of=as_of, expected=generation)
            except FinCLError as exc:
                if str(exc).startswith('STALE_GENERATION'):
                    raise HTTPException(409, str(exc)) from exc
                raise HTTPException(404 if 'does not exist' in str(exc) else 422, str(exc)) from exc

    @app.get('/api/v1/debts/{debt_id}/events')
    def debt_event_list(debt_id: str, cursor: str | None = None, limit: int = 100,
                        generation: str | None = None) -> Any:
        from .debts.queries import debt_events
        with ledger_writer(database):
            try:
                return debt_events(database, debt_id, cursor=cursor, limit=limit,
                                   expected=generation)
            except FinCLError as exc:
                if str(exc).startswith('STALE_GENERATION'):
                    raise HTTPException(409, str(exc)) from exc
                raise HTTPException(404 if 'does not exist' in str(exc) else 422, str(exc)) from exc

    @app.post('/api/v1/preview')
    def preview(command: Command) -> Any:
        with ledger_writer(database):
            extra = _validate(database,command)
            note = ('Refund allocation affects attributed and receipt-period cost, never the recorded cash flow or its date.' if command.kind.startswith('REFUND')
                    else 'A confirmed duplicate removes the candidate from every total.' if command.kind.startswith('DUPLICATE')
                    else 'Categories do not change cash-flow classification.' if command.kind.startswith('CATEGORY')
                    else 'A confirmed transfer removes both legs from spending and income.' if command.kind == 'TRANSFER_DECISION'
                    else 'Review these values before saving.')
            result = {'valid':True,'generation':command.expected_generation,'effect':command.payload,'note':note}
            if command.kind == 'REFUND_ALLOCATION_SET':
                result['totals'] = extra
            return result

    @app.post('/api/v1/commands')
    def submit(command: Command) -> Any:
        intent = hashlib.sha256(canonical_json({'kind':command.kind,'anchors':command.anchors,'payload':command.payload}).encode()).hexdigest()
        with ledger_writer(database) as writer:
            records = read_records(default_journal_path(database))
            prior = next((r for r in records if r.command and r.command['request_id']==str(command.request_id)),None)
            kind: str = command.kind
            anchors: dict[str,str] = dict(command.anchors)
            payload: dict[str,object] = dict(command.payload)
            policy = 'dashboard-v1'
            if prior:
                if prior.command and prior.command['intent_hash'] != intent:
                    raise HTTPException(409,'REQUEST_ID_CONFLICT')
            else:
                extra = _validate(database,command)
                if command.kind == 'TRANSFER_DECISION':
                    kind = 'LINK_CONFIRM' if command.payload['decision'] == 'confirm' else 'LINK_REJECT'
                    anchors = {'left':command.anchors['left'],'right':command.anchors['right']}
                    payload = {'reason':command.payload['reason'],'link_kind':extra['link_kind']}
                    policy = extra.get('policy_version',policy)
                elif command.kind in _DEBT_KINDS:
                    policy = 'personal-debt-v1'
                elif command.kind in _CARD_KINDS:
                    policy = 'card-facility-v1'
                elif command.kind in _INSTALLMENT_KINDS:
                    policy = 'installment-v1'
            receipt = record_decision(database,request_id=str(command.request_id),intent_hash=intent,decision_kind=kind,anchors=anchors,payload=payload,policy_version=policy,snapshot=f'{kind}: {payload}',writer=writer)
            return JSONResponse({**asdict(receipt),'status':'recorded_refresh_pending' if receipt.refresh_pending else 'applied'},status_code=202 if receipt.refresh_pending else 200)

    @app.post('/api/v1/batch')
    def batch(body: Batch) -> Any:
        items: list[dict[str,object]] = [
            {'kind':i.kind,'anchors':dict(i.anchors),'payload':dict(i.payload)} for i in body.items
        ]
        intent = hashlib.sha256(canonical_json({'items':items,'reason':body.reason}).encode()).hexdigest()
        with ledger_writer(database) as writer:
            records = read_records(default_journal_path(database))
            prior = next((r for r in records if r.command and r.command['request_id']==str(body.request_id)),None)
            if prior:
                if prior.command and prior.command['intent_hash'] != intent:
                    raise HTTPException(409,'REQUEST_ID_CONFLICT')
            else:
                connection = connect_readonly(database)
                try:
                    generation = current_generation(connection)
                    if generation is None or generation['generation_token'] != body.expected_generation:
                        raise HTTPException(409,'STALE_GENERATION: reload and preview again. Your draft is preserved.')
                finally:
                    connection.close()
            receipt = record_batch(database,request_id=str(body.request_id),intent_hash=intent,items=items,reason=body.reason,writer=writer)
            return JSONResponse({**asdict(receipt),'status':'recorded_refresh_pending' if receipt.refresh_pending else 'applied'},status_code=202 if receipt.refresh_pending else 200)

    @app.get('/api/v1/relationships/review',response_model=RelationshipsReview)
    def relationships_review(kind: str = 'duplicate', generation: str | None = None, cursor: str | None = None, limit: int = 50) -> Any:
        from .queries import relationships_review as _review
        with ledger_writer(database):
            try:
                return _review(database, kind=kind, generation=generation, cursor=cursor, limit=limit)
            except FinCLError as exc:
                if str(exc).startswith('STALE_GENERATION'):
                    raise HTTPException(409, str(exc)) from exc
                raise

    @app.get('/api/v1/history')
    def history() -> Any:
        with ledger_writer(database):
            records = read_records(default_journal_path(database))
            return [{'sequence':r.seq,'date':r.decided_at,'kind':r.decision_kind,
                     'reason':str(r.payload.get('reason','')),
                     'category':str(r.payload.get('category','')),
                     'request_id':r.command['request_id'] if r.command else None}
                    for r in reversed(records[-50:])]

    @app.get('/api/v1/receipts/{request_id}')
    def receipt_status(request_id: UUID) -> Any:
        with ledger_writer(database):
            records = read_records(default_journal_path(database))
            record = next((r for r in records if r.command and r.command['request_id']==str(request_id)),None)
            if record is None:
                raise HTTPException(404,'No durable decision found for this request')
            connection = connect_readonly(database)
            try:
                generation = current_generation(connection)
                applied = generation is not None and json.loads(generation['manifest_json'])['journal_seq'] >= record.seq
                return {'request_id':str(request_id),'sequence':record.seq,'status':'applied' if applied else 'recorded_refresh_pending'}
            finally:
                connection.close()

    @app.get('/api/v1/evidence/{entry_id}')
    def evidence(entry_id: str, generation: str) -> Any:
        from .downloads import evidence_response
        return evidence_response(database, evidence_dir or database.parent / 'evidence', entry_id, generation)

    @app.post('/api/v1/refresh')
    def refresh() -> Any:
        refresh_ledger(database)
        return {'status':'refreshed'}

    from .api_cards import register_card_reads
    register_card_reads(app, database)

    if assets.exists():
        if (assets / 'assets').exists():
            app.mount('/assets',StaticFiles(directory=assets / 'assets'),name='assets')
        @app.get('/')
        def index() -> FileResponse:
            return FileResponse(assets / 'index.html')
    return app
