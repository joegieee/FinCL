"""Authenticated loopback HTTP application. Financial state lives in the ledger/journal."""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .application import record_decision
from .common import canonical_json, resolve_category
from .custom_categories import custom_categories, validate_creation
from .db import connect_readonly
from .decision_journal import default_journal_path, read_records
from .errors import FinCLError
from .generation import build_manifest, current_generation, generation_token
from .locking import LedgerBusyError, ledger_writer
from .plans import validate_plan
from .queries import analysis_page, contributors_page, dashboard, transactions_page
from .refresh import refresh_ledger


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Command(StrictModel):
    request_id: UUID
    expected_generation: str = Field(min_length=1,max_length=100)
    kind: Literal['CATEGORY_OVERRIDE','CATEGORY_CREATE','PLAN_SET','INBOUND_CLASSIFICATION','TRANSFER_DECISION']
    anchors: dict[str,str]
    payload: dict[str,str]


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
        if request.method not in ('GET','HEAD') and (request.headers.get('origin') != origin or request.headers.get('content-type','').split(';')[0] != 'application/json'):
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

    @app.post('/api/v1/preview')
    def preview(command: Command) -> Any:
        with ledger_writer(database):
            _validate(database,command)
            note = ('Categories do not change cash-flow classification.' if command.kind.startswith('CATEGORY')
                    else 'A confirmed transfer removes both legs from spending and income.' if command.kind == 'TRANSFER_DECISION'
                    else 'Review these values before saving.')
            return {'valid':True,'generation':command.expected_generation,'effect':command.payload,'note':note}

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
            receipt = record_decision(database,request_id=str(command.request_id),intent_hash=intent,decision_kind=kind,anchors=anchors,payload=payload,policy_version=policy,snapshot=f'{kind}: {payload}',writer=writer)
            return JSONResponse({**asdict(receipt),'status':'recorded_refresh_pending' if receipt.refresh_pending else 'applied'},status_code=202 if receipt.refresh_pending else 200)

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

    if assets.exists():
        if (assets / 'assets').exists():
            app.mount('/assets',StaticFiles(directory=assets / 'assets'),name='assets')
        @app.get('/')
        def index() -> FileResponse:
            return FileResponse(assets / 'index.html')
    return app
