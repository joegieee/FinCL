"""Authenticated loopback HTTP application. Financial state lives in the ledger/journal."""
from __future__ import annotations

import hashlib
import json
import secrets
import shutil
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from . import clock
from .application import record_batch, record_decision
from .common import canonical_json, resolve_category
from .custom_categories import custom_categories, validate_creation
from .db import connect_readonly
from .decision_journal import default_journal_path, read_records
from .decisions import resolve_anchor
from .errors import FinCLError
from .generation import build_manifest, current_generation, generation_token
from .locking import LedgerBusyError, ledger_writer
from .money import ExactMoney, exact_sum, format_decimal
from .planning import PLANNING_DECISION_KINDS
from .plans import validate_plan
from .position import POSITION_DECISION_KINDS
from .queries import analysis_page, contributors_page, dashboard, transactions_page
from .refresh import refresh_ledger


def _absolute_money(value: str) -> ExactMoney:
    """Parse a displayed exact decimal without routing it through float."""
    whole, _, fraction = value.lstrip('+-').partition('.')
    return ExactMoney(abs(int((whole or '0') + fraction)), len(fraction), '')


def _generic_entries(connection: Any) -> list[dict[str, Any]]:
    """Raw entry-view rows reshaped for the generic ``financial_roles._matches``
    predicate, shared by the category/role/inbound rule compilers so a combined
    rule's per-kind impact reuses one match evaluation."""
    from .queries import _ENTRY_VIEW
    rows = []
    for row in connection.execute(f'SELECT * FROM ({_ENTRY_VIEW})').fetchall():
        raw = dict(row)
        rows.append({**raw, 'account_id': raw['role_account_id'], 'description_text': raw['role_description_text'],
                     'paynow_to': raw['role_paynow_to'], 'merchant_match': raw['role_merchant_match']})
    return rows


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
    # v0.6 planning kinds
    'BUDGET_REVISION_SET','BUDGET_ARCHIVE_SET','GOAL_DETAILS_SET',
    'GOAL_OBSERVATION_SET','GOAL_OBSERVATION_RETIRE','GOAL_ARCHIVE_SET',
    'RECURRING_COMMITMENT_SET','RECURRING_CANDIDATE_DISMISS',
    'RECURRING_OCCURRENCE_SET','RECURRING_OCCURRENCE_RETIRE',
    # Stage 2A liquid-position observations
    'BALANCE_OBSERVATION_SET','BALANCE_OBSERVATION_RETIRE',
    # Cash-flow coverage: operator attestation that a quiet range is complete
    'MOVEMENT_COVERAGE_CERTIFY','MOVEMENT_COVERAGE_RETIRE',
    'ENTRY_ROLE_SET','ENTRY_ROLE_RETIRE','ROLE_RULE_SET','ROLE_RULE_RETIRE',
    # M5 reusable inbound-classification rules
    'INBOUND_RULE_SET','INBOUND_RULE_RETIRE',
    # Reporting-currency plan M1
    'REPORTING_CURRENCY_SET',
    # Stage 5A forecast-actual matching: binding real cash to an issued event
    'FORECAST_MATCH_SET','FORECAST_MATCH_RETIRE',
    # Stage 4A sinking-fund allocation controls
    'SINKING_FUND_ALLOCATION_SET','SINKING_FUND_ALLOCATION_RETIRE',
    # The chosen reserve policy (a month count; never an amount)
    'RESERVE_POLICY_SET',
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


class RuleDraft(StrictModel):
    match: dict[str,Any]
    category: str = Field(min_length=1,max_length=200)
    reason: str = Field(min_length=1,max_length=2000)


class CategorizationImpactDraft(RuleDraft):
    generation: str = Field(min_length=1, max_length=100)
    alias_canonical: str | None = Field(default=None, min_length=1, max_length=200)
    alias_patterns: list[dict[str, str]] | None = None


class RuleDerivationDraft(StrictModel):
    generation: str = Field(min_length=1, max_length=100)
    anchors: list[str] = Field(min_length=1, max_length=200)


class MerchantMergeDraft(StrictModel):
    generation: str = Field(min_length=1, max_length=100)
    sources: list[str] = Field(min_length=1, max_length=50)
    target: str = Field(min_length=1, max_length=200)
    display_name: str | None = Field(default=None, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)
    resolutions: dict[str, str] = Field(default_factory=dict)


class MerchantRenameDraft(StrictModel):
    generation: str = Field(min_length=1, max_length=100)
    merchant_key: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=1, max_length=2000)


class BatchItem(StrictModel):
    kind: Literal[
        'CATEGORY_OVERRIDE','CATEGORY_OVERRIDE_RETIRE','INBOUND_CLASSIFICATION',
        'DUPLICATE_CONFIRM','DUPLICATE_REJECT','DUPLICATE_RETIRE',
    'MERCHANT_ALIAS_SET','MERCHANT_ALIAS_RETIRE','CATEGORY_RULE_SET','CATEGORY_RULE_RETIRE',
        'ENTRY_ROLE_SET','ENTRY_ROLE_RETIRE','ROLE_RULE_SET','ROLE_RULE_RETIRE',
        'INBOUND_RULE_SET','INBOUND_RULE_RETIRE',
        # A split bill: new receivables, each person's share, and the owner's
        # own share, recorded together or not at all.
        'PERSONAL_DEBT_OPEN','PERSONAL_DEBT_EVENT_SET','ENTRY_RESIDUAL_SET',
    ]
    anchors: dict[str,str]
    payload: dict[str,Any]


class Batch(StrictModel):
    request_id: UUID
    expected_generation: str = Field(min_length=1,max_length=100)
    # A transaction may receive both a category and a role decision.  The
    # frozen selection is capped at 200, so its one atomic batch needs room
    # for both child records.
    items: list[BatchItem] = Field(min_length=1,max_length=400)
    reason: str = Field(max_length=2000)


class Bootstrap(StrictModel):
    secret: str = Field(max_length=256)


class CategoryCutDraft(StrictModel):
    category_id: str = Field(min_length=1, max_length=300)
    monthly_reduction: str = Field(min_length=1, max_length=40)


class EventDateChangeDraft(StrictModel):
    event_id: str = Field(min_length=1, max_length=500)
    new_date: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')


class HypotheticalIncomeDraft(StrictModel):
    amount: str = Field(min_length=1, max_length=40)
    currency: str = Field(pattern=r'^[A-Z]{3}$')
    on: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    label: str = Field(default='Hypothetical income', min_length=1, max_length=120)


class SpendingWhatIfDraft(StrictModel):
    month: str = Field(pattern=r'^\d{4}-\d{2}$')
    currency: str = Field(pattern=r'^[A-Z]{3}$')
    category_cuts: list[CategoryCutDraft] = Field(default_factory=list, max_length=20)
    date_changes: list[EventDateChangeDraft] = Field(default_factory=list, max_length=20)
    hypothetical_income: list[HypotheticalIncomeDraft] = Field(default_factory=list, max_length=20)


class FundConfirmationDraft(StrictModel):
    proposal_id: str = Field(min_length=1, max_length=500)
    generation: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)


class SnapshotDraft(StrictModel):
    generation: str = Field(min_length=1, max_length=100)
    currency: str = Field(pattern=r'^[A-Z]{3}$')
    case: Literal['base', 'conservative'] = 'base'
    reason: str = Field(min_length=1, max_length=2000)


class BackupActionDraft(StrictModel):
    backup_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9-]{1,100}$')


class FinancialRoleImpactDraft(StrictModel):
    generation: str = Field(min_length=1, max_length=100)
    anchors: list[str] = Field(min_length=1, max_length=200)
    category: str | None = None
    clear_category: bool = False
    cost_behavior: str | None = None
    need: str | None = None
    clear_roles: bool = False


class FieldEditIn(StrictModel):
    intent: Literal['keep','set']
    value: str | None = None


class ClassificationDraftIn(StrictModel):
    """Step 1 unified draft: one explicit keep/set intent per field, applied to
    a frozen anchor selection. See ``fincl.classification`` for the semantics."""
    generation: str = Field(min_length=1, max_length=100)
    anchors: list[str] = Field(min_length=1, max_length=200)
    category: FieldEditIn | None = None
    cost_behavior: FieldEditIn | None = None
    need: FieldEditIn | None = None
    incoming_type: FieldEditIn | None = None
    clear_category: bool = False
    clear_roles: bool = False
    policy: Literal['fill_missing','replace'] = 'replace'
    reason: str = Field(default='', max_length=2000)


class RoleRuleDraft(StrictModel):
    generation: str = Field(min_length=1, max_length=100)
    match: dict[str, Any]
    cost_behavior: str | None = None
    need: str | None = None
    reason: str = Field(min_length=1, max_length=2000)


class InboundRuleDraft(StrictModel):
    generation: str = Field(min_length=1, max_length=100)
    match: dict[str, Any]
    classification: str = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=1, max_length=2000)


class CombinedRuleDraft(StrictModel):
    generation: str = Field(min_length=1, max_length=100)
    match: dict[str, Any]
    category: str | None = Field(default=None, max_length=200)
    cost_behavior: str | None = None
    need: str | None = None
    incoming_type: str | None = None
    reason: str = Field(min_length=1, max_length=2000)


class CombinedRetireDraft(StrictModel):
    generation: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)


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
    cost_behavior: str
    cost_behavior_basis: str
    need: str
    need_basis: str
    role_review_state: str
    suggested_classification: str
    linked: bool
    classification: str | None
    # 'full' / 'partial' when personal-debt allocations explain all / some of
    # this movement's amount (`queries._DEBT_ALLOCATION_SQL`); a full one is
    # bucket 'debt'.
    debt_allocation: str | None


class TransactionEntry(StrictModel):
    generation: str
    entry: Entry


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
        reason = command.payload.get('reason', '')
        if not isinstance(reason, str) or len(reason) > 2000 or (
            command.kind != 'CATEGORY_OVERRIDE' and not reason.strip()
        ):
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
            from .planning import plan_has_dated_records
            if plan_has_dated_records(connection, command.anchors['plan']):
                raise FinCLError('This budget/goal now uses dated revisions. Edit it there instead of the legacy plan form.')
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
                    custom_categories=custom_categories(database),
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
        elif command.kind in PLANNING_DECISION_KINDS:
            from .planning import validate_planning
            validate_planning(command.kind, command.anchors, command.payload)
        elif command.kind in POSITION_DECISION_KINDS:
            from .position import validate_position_payload
            validate_position_payload(command.kind, command.anchors, command.payload)
            if command.kind in {'BALANCE_OBSERVATION_SET','MOVEMENT_COVERAGE_CERTIFY'}:
                account_id = str(command.payload['account_id'])
                if not connection.execute(
                    'SELECT 1 FROM accounts WHERE id=?', (account_id,)
                ).fetchone():
                    raise FinCLError('The selected account no longer exists')
            elif command.kind == 'MOVEMENT_COVERAGE_RETIRE':
                if not connection.execute(
                    'SELECT 1 FROM movement_coverage_certifications WHERE certification=? AND retired=0',
                    (command.anchors['certification'],),
                ).fetchone():
                    raise FinCLError('The coverage certification is not active')
            elif not connection.execute(
                'SELECT 1 FROM balance_observations WHERE observation=? AND retired=0',
                (command.anchors['observation'],),
            ).fetchone():
                raise FinCLError('The balance observation is not active')
        elif command.kind in {'ENTRY_ROLE_SET','ENTRY_ROLE_RETIRE','ROLE_RULE_SET','ROLE_RULE_RETIRE'}:
            from .financial_roles import validate_financial_role
            validate_financial_role(command.kind, dict(command.anchors), dict(command.payload))
            if 'entry' in command.anchors and not connection.execute('SELECT 1 FROM ledger_entries WHERE natural_anchor=?', (command.anchors['entry'],)).fetchone():
                raise FinCLError('The transaction no longer resolves')
        elif command.kind in {'SINKING_FUND_ALLOCATION_SET','SINKING_FUND_ALLOCATION_RETIRE'}:
            from .funds import validate_fund_payload
            validate_fund_payload(command.kind, dict(command.anchors), dict(command.payload))
            if not connection.execute(
                'SELECT 1 FROM sinking_funds WHERE fund=?', (command.anchors['fund'],)
            ).fetchone():
                raise FinCLError('That fund no longer exists')
            if command.kind == 'SINKING_FUND_ALLOCATION_RETIRE' and not connection.execute(
                'SELECT 1 FROM sinking_fund_allocations WHERE allocation=? AND retired=0',
                (command.anchors['allocation'],),
            ).fetchone():
                raise FinCLError('That allocation is not active')
        elif command.kind == 'RESERVE_POLICY_SET':
            from .reserve import validate_reserve_payload
            validate_reserve_payload(command.kind, dict(command.anchors), dict(command.payload))
        elif command.kind in {'FORECAST_MATCH_SET','FORECAST_MATCH_RETIRE'}:
            from .variance import validate_variance_payload
            validate_variance_payload(command.kind, dict(command.anchors), dict(command.payload))
            if not connection.execute(
                'SELECT 1 FROM forecast_snapshots WHERE snapshot=?',
                (command.anchors['snapshot'],),
            ).fetchone():
                raise FinCLError('The forecast snapshot no longer exists')
            if command.kind == 'FORECAST_MATCH_RETIRE' and not connection.execute(
                'SELECT 1 FROM forecast_actual_matches WHERE snapshot=? AND match=? AND retired=0',
                (command.anchors['snapshot'], command.anchors['match']),
            ).fetchone():
                raise FinCLError('The match is not active')
        elif command.kind in {'INBOUND_RULE_SET','INBOUND_RULE_RETIRE'}:
            from .inbound_rules import validate_inbound_rule
            validate_inbound_rule(command.kind, dict(command.anchors), dict(command.payload))
        elif command.kind == 'REPORTING_CURRENCY_SET':
            from .reporting_currency import (
                currency_coverage_preview,
                validate_reporting_currency_payload,
            )
            validate_reporting_currency_payload(command.kind, dict(command.anchors), dict(command.payload))
            preview = currency_coverage_preview(connection, str(command.payload['currency']))
            if preview['structurally_unconfigured']:
                raise FinCLError(
                    'No direct or inverse rate path is configured for: '
                    + ', '.join(preview['structurally_unconfigured'])  # type: ignore[arg-type]
                )
            extra = {'coverage_preview': json.dumps(preview)}
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


def create_app(database: Path, *, port: int = 8765, public_host: str = '127.0.0.1', bootstrap_secret: str | None = None, require_access_key: bool = False, assets: Path | None = None, evidence_dir: Path | None = None, demo: bool = False, today: date | None = None) -> FastAPI:
    # `today` pins this process's notion of the current date (see `fincl.clock`).
    # It exists for the synthetic browser fixtures, whose dates are fixed: a
    # suite that read the real clock would stop agreeing with them as time
    # passed. It is a constructor argument on purpose -- no request and no
    # environment variable can reach it.
    clock.pin(today)
    database = database.resolve()
    app = FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
    app.state.bootstrap_secret = bootstrap_secret or secrets.token_urlsafe(32)
    app.state.session = secrets.token_urlsafe(32)
    app.state.csrf = secrets.token_urlsafe(32)
    # Preserve the one-use gate for opt-in use; normal serving is key-free.
    app.state.require_access_key = require_access_key
    app.state.bootstrapped = False
    origin = f'http://{public_host}:{port}'
    cookie_name = f'fincl_session_{port}'
    assets = assets or Path(__file__).parent / 'static'

    @app.middleware('http')
    async def boundary(request: Request, call_next: Any) -> Any:
        if request.headers.get('host') != f'{public_host}:{port}':
            return JSONResponse({'detail':'Invalid host'},status_code=403)
        if request.headers.get('origin') not in (None,origin):
            return JSONResponse({'detail':'Invalid origin'},status_code=403)
        content_type = request.headers.get('content-type','').split(';')[0]
        allowed_write_type = content_type == 'application/json' or (
            request.url.path in ('/api/v1/imports','/api/v1/fx-rates') and content_type == 'multipart/form-data'
        )
        if request.method not in ('GET','HEAD') and (request.headers.get('origin') != origin or not allowed_write_type):
            return JSONResponse({'detail':'Same-origin JSON is required'},status_code=403)
        if request.url.path.startswith('/api/'):
            if app.state.require_access_key and (not app.state.bootstrapped or not secrets.compare_digest(request.cookies.get(cookie_name,''),app.state.session)):
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
        if not app.state.require_access_key:
            return JSONResponse({'csrf':app.state.csrf})
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

    @app.post('/api/v1/fx-rates')
    def fx_rates_create(
        file: UploadFile = File(...),
        mas_weekly_csv: bool = Form(False),
        source_version: str = Form(''),
    ) -> Any:
        from .db import connect_sqlite, initialize_schema
        from .fx import load_fx_rates, parse_mas_weekly_csv
        raw = file.file.read()
        file.file.close()
        if mas_weekly_csv:
            if not source_version.strip():
                raise FinCLError('--source-version is required with --mas-weekly-csv')
            payload = parse_mas_weekly_csv(raw.decode('utf-8-sig'), source_version=source_version.strip())
        else:
            try:
                payload = json.loads(raw)
            except ValueError as exc:
                raise FinCLError(f'cannot read fx rate document: {exc}') from exc
            if not isinstance(payload, dict):
                raise FinCLError('fx rate document must be a JSON object')
        with ledger_writer(database):
            connection = connect_sqlite(database)
            try:
                initialize_schema(connection)
                written = load_fx_rates(connection, payload)
            finally:
                connection.close()
        return {'status':'LOADED','rates_written':written}

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

    @app.get('/api/v1/baseline/readiness')
    def baseline_readiness_read() -> Any:
        from .baseline import baseline_readiness
        return baseline_readiness(database, today=clock.today())

    @app.get('/api/v1/baseline/cost-income')
    def baseline_cost_income_read() -> Any:
        from .baseline import cost_income_table
        return cost_income_table(database, today=clock.today())

    @app.get('/api/v1/baseline/roles')
    def baseline_roles_read() -> Any:
        from .baseline import baseline_roles
        return baseline_roles(database, today=clock.today())

    @app.get('/api/v1/baseline/estimate')
    def baseline_estimate_read() -> Any:
        from .baseline import baseline_estimate
        return baseline_estimate(database, today=clock.today())

    @app.get('/api/v1/obligations')
    def obligations_read(as_of: str | None = None) -> Any:
        from .obligations import obligations_page
        return obligations_page(database, as_of=as_of, today=clock.today())

    @app.get('/api/v1/forecast/manifest')
    def forecast_manifest_read(as_of: str | None = None) -> Any:
        from .forecast import forecast_input_manifest
        return forecast_input_manifest(database, today=clock.today(), as_of=as_of)

    @app.get('/api/v1/forecast')
    def forecast_read() -> Any:
        """Read the Stage 3B timeline without changing its construction or arithmetic."""
        from .forecast import cash_timeline
        return cash_timeline(database, today=clock.today())

    @app.get('/api/v1/funds')
    def funds_read(as_of: str | None = None) -> Any:
        from .funds import sinking_funds_page
        return sinking_funds_page(database, as_of=as_of)

    @app.post('/api/v1/funds/confirm')
    def fund_confirm(body: FundConfirmationDraft) -> Any:
        """Confirm a complete evidence proposal without asking for an amount."""
        from calendar import monthrange

        from .funds import SINKING_FUND_SET, sinking_funds_page, validate_fund_payload

        with ledger_writer(database) as writer:
            connection = connect_readonly(database)
            try:
                generation = current_generation(connection)
                if generation is None or generation['generation_token'] != body.generation:
                    raise HTTPException(409, 'STALE_GENERATION: reload and confirm again.')
            finally:
                connection.close()
            page = sinking_funds_page(database)
            proposal = next((p for p in page['proposals'] if p['proposal_id'] == body.proposal_id), None)
            if proposal is None:
                raise HTTPException(404, 'This fund proposal is no longer available.')
            if not proposal['complete']:
                raise HTTPException(422, 'Missing evidence must be resolved before this fund can be confirmed.')
            due_on = str(proposal['observed_date']['value'])
            cursor = clock.today().replace(day=1)
            due = date.fromisoformat(due_on)
            contribution_dates: list[str] = []
            while cursor <= due:
                day = min(due.day, monthrange(cursor.year, cursor.month)[1])
                candidate = cursor.replace(day=day)
                if candidate >= clock.today() and candidate <= due:
                    contribution_dates.append(candidate.isoformat())
                cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
            fund_id = f"fund-{hashlib.sha256(body.proposal_id.encode()).hexdigest()[:20]}"
            payload: dict[str, object] = {
                'name': proposal['label'], 'currency': proposal['currency'],
                'amount_due': proposal['observed_amount']['value'], 'due_on': due_on,
                'amount_basis': proposal['amount_basis'], 'date_basis': proposal['date_basis'],
                'source_reference': proposal['proposal_id'],
                'contribution_dates': contribution_dates,
                'linked_event_id': proposal['source'].get('obligation_id'), 'reason': body.reason,
            }
            anchors = {'fund': fund_id}
            validate_fund_payload(SINKING_FUND_SET, anchors, payload)
            intent = hashlib.sha256(canonical_json({'kind': SINKING_FUND_SET, 'anchors': anchors, 'payload': payload}).encode()).hexdigest()
            receipt = record_decision(
                database, request_id=str(uuid4()), intent_hash=intent,
                decision_kind=SINKING_FUND_SET, anchors=anchors, payload=payload,
                policy_version='sinking-fund-v1', snapshot=f'Confirmed sourced fund: {proposal["label"]}',
                writer=writer,
            )
            return {'status': 'recorded_refresh_pending' if receipt.refresh_pending else 'applied', 'fund': fund_id}

    @app.get('/api/v1/planning/sufficiency')
    def planning_sufficiency_read() -> Any:
        """Will there be enough money, when it runs short, and what drove it."""
        from .planning.sufficiency import sufficiency_page
        return sufficiency_page(database, today=clock.today())

    @app.get('/api/v1/reserve/policy')
    def reserve_policy_read() -> Any:
        """The chosen reserve policy, or the framework default if none was chosen."""
        from .db import connect_readonly as _open
        from .reserve import reserve_policy
        connection = _open(database)
        try:
            policy = reserve_policy(connection)
        finally:
            connection.close()
        return {
            'months': str(policy.months), 'is_default': policy.is_default,
            'decided_at': policy.decided_at, 'policy_version': policy.policy_version,
            'reason': policy.reason,
        }

    @app.get('/api/v1/forecast/snapshots')
    def forecast_snapshots_read() -> Any:
        from .variance import forecast_snapshots_page
        return forecast_snapshots_page(database)

    @app.post('/api/v1/forecast/snapshots')
    def forecast_snapshot_issue(body: SnapshotDraft) -> Any:
        from .variance import FORECAST_SNAPSHOT_ISSUE, snapshot_payload, validate_variance_payload
        with ledger_writer(database) as writer:
            connection = connect_readonly(database)
            try:
                generation = current_generation(connection)
                if generation is None or generation['generation_token'] != body.generation:
                    raise HTTPException(409, 'STALE_GENERATION: reload and save the snapshot again.')
            finally:
                connection.close()
            snapshot_id = f"snapshot-{uuid4().hex}"
            anchors, payload = snapshot_payload(
                database, snapshot=snapshot_id, currency=body.currency,
                reason=body.reason, case=body.case,
            )
            validate_variance_payload(FORECAST_SNAPSHOT_ISSUE, anchors, payload)
            intent = hashlib.sha256(canonical_json({'kind': FORECAST_SNAPSHOT_ISSUE, 'anchors': anchors, 'payload': payload}).encode()).hexdigest()
            receipt = record_decision(
                database, request_id=str(uuid4()), intent_hash=intent,
                decision_kind=FORECAST_SNAPSHOT_ISSUE, anchors=anchors, payload=payload,
                policy_version='forecast-variance-v1', snapshot=f'Issued {snapshot_id}', writer=writer,
            )
            return {'status': 'recorded_refresh_pending' if receipt.refresh_pending else 'applied', 'snapshot': snapshot_id}

    @app.get('/api/v1/forecast/variance/{snapshot_id}')
    def forecast_variance_read(snapshot_id: str, as_of: str | None = None) -> Any:
        from .variance import forecast_variance_page
        return forecast_variance_page(database, snapshot=snapshot_id, as_of=as_of)

    @app.get('/api/v1/forecast/validation')
    def forecast_validation_read() -> Any:
        """Read the Stage 3C temporal-holdout validation result."""
        from .forecast import forecast_validation
        return forecast_validation(database)

    @app.get('/api/v1/spending-controls')
    def spending_controls_read(
        month: str | None = None, currency: str = Query('SGD', pattern=r'^[A-Z]{3}$')
    ) -> Any:
        from .controls import spending_controls_page
        return spending_controls_page(database, month=month, currency=currency, today=clock.today())

    @app.post('/api/v1/spending-controls/what-if')
    def spending_controls_scenario(body: SpendingWhatIfDraft) -> Any:
        from .controls import spending_controls_what_if
        return spending_controls_what_if(
            database,
            month=body.month,
            currency=body.currency,
            category_cuts=[item.model_dump() for item in body.category_cuts],
            date_changes=[item.model_dump() for item in body.date_changes],
            hypothetical_income=[item.model_dump() for item in body.hypothetical_income],
            today=clock.today(),
        )

    @app.get('/api/v1/position')
    def position_read() -> Any:
        from .position import position_page
        return position_page(database, today=clock.today())

    @app.get('/api/v1/position/coverage')
    def position_coverage_read() -> Any:
        """Coverage gaps and the exact evidence actions that would close them."""
        from .position import coverage_remedies
        return coverage_remedies(database, today=clock.today())

    @app.get('/api/v1/position/certifications')
    def position_certifications_read() -> Any:
        """Active operator attestations that a quiet range has complete evidence."""
        from .position import movement_certifications
        return movement_certifications(database)

    @app.get('/api/v1/net-worth')
    def net_worth_read(as_of: str | None = None, prior_as_of: str | None = None) -> Any:
        from .networth import net_worth_page
        return net_worth_page(database, as_of=as_of, prior_as_of=prior_as_of, today=clock.today())

    @app.get('/api/v1/categories')
    def categories_read() -> Any:
        from .v05 import categories_page
        return categories_page(database)

    @app.get('/api/v1/rules')
    def rules_read() -> Any:
        from .v05 import rules_page
        return rules_page(database)

    @app.get('/api/v1/rules/{rule_id}/history')
    def rule_history_read(rule_id: str) -> Any:
        from .v05 import rule_history
        return rule_history(database, rule_id)

    @app.get('/api/v1/rules/{rule_id}/retirement-impact')
    def rule_retirement_impact(rule_id: str, generation: str) -> Any:
        from .engine import preview_rule_retirement_impact
        connection = connect_readonly(database)
        try:
            current = current_generation(connection)
            if current is None or current['generation_token'] != generation:
                raise HTTPException(409, 'STALE_GENERATION: reload and preview again. Your draft is preserved.')
            try:
                impact = preview_rule_retirement_impact(connection, rule_id=rule_id)
            except ValueError as exc:
                raise HTTPException(404, str(exc)) from exc
            return {'generation': generation, 'impact': impact}
        finally:
            connection.close()

    @app.get('/api/v1/aliases/{canonical}/history')
    def alias_history_read(canonical: str) -> Any:
        from .v05 import alias_history
        return alias_history(database, canonical)

    @app.get('/api/v1/categorization/summary')
    def categorization_summary(generation: str) -> Any:
        from .categorization_queries import categorization_summary as read_summary
        try:
            return read_summary(database, generation=generation)
        except FinCLError as exc:
            if str(exc).startswith('STALE_GENERATION'):
                raise HTTPException(409, str(exc)) from exc
            raise

    @app.get('/api/v1/categorization/candidates')
    def categorization_candidates(generation: str, cursor: str | None = None,
                                  limit: int = Query(50, ge=1, le=100), account: str = '',
                                  currency: str = '', direction: str = '', lifecycle: str = '', kind: str = '',
                                  focus: str = 'category', sort: str = 'active') -> Any:
        from .categorization_queries import categorization_candidates as read_candidates
        try:
            return read_candidates(
                database, generation=generation, cursor=cursor, limit=limit, account=account,
                currency=currency, direction=direction, lifecycle=lifecycle, kind=kind,
                focus=focus, sort=sort,
            )
        except FinCLError as exc:
            if str(exc).startswith('STALE_GENERATION'):
                raise HTTPException(409, str(exc)) from exc
            raise

    @app.get('/api/v1/categorization/candidates/{candidate_id}')
    def categorization_candidate_detail(candidate_id: str, generation: str, cursor: str | None = None,
                                        limit: int = Query(50, ge=1, le=100), account: str = '', currency: str = '',
                                        direction: str = '', lifecycle: str = '', kind: str = '',
                                        focus: str = 'category') -> Any:
        from .categorization_queries import categorization_candidate_detail as read_detail
        try:
            return read_detail(
                database, generation=generation, candidate_id=candidate_id, cursor=cursor, limit=limit,
                account=account, currency=currency, direction=direction, lifecycle=lifecycle, kind=kind,
                focus=focus,
            )
        except FinCLError as exc:
            if str(exc).startswith('STALE_GENERATION'):
                raise HTTPException(409, str(exc)) from exc
            if str(exc) == 'Categorization candidate not found':
                raise HTTPException(404, str(exc)) from exc
            raise

    @app.post('/api/v1/categorization/derive-rule')
    def categorization_derive_rule(draft: RuleDerivationDraft) -> Any:
        from .categorization_queries import derive_rule_options
        try:
            return derive_rule_options(database, generation=draft.generation, anchors=draft.anchors)
        except FinCLError as exc:
            if str(exc).startswith('STALE_GENERATION'):
                raise HTTPException(409, str(exc)) from exc
            raise

    @app.get('/api/v1/categorization/rule-suggestions')
    def categorization_rule_suggestions(generation: str) -> Any:
        from .categorization_queries import rule_suggestions
        try:
            return rule_suggestions(database, generation=generation)
        except FinCLError as exc:
            if str(exc).startswith('STALE_GENERATION'):
                raise HTTPException(409, str(exc)) from exc
            raise

    @app.post('/api/v1/categorization/merchant-merge/preview')
    def categorization_merchant_merge_preview(draft: MerchantMergeDraft) -> Any:
        from .categorization_queries import merchant_merge_preview
        try:
            return merchant_merge_preview(database, generation=draft.generation, sources=draft.sources,
                                          target=draft.target, display_name=draft.display_name, reason=draft.reason,
                                          resolutions=draft.resolutions)
        except FinCLError as exc:
            if str(exc).startswith('STALE_GENERATION'):
                raise HTTPException(409, str(exc)) from exc
            raise

    @app.get('/api/v1/merchants')
    def merchants_search(generation: str, q: str = '', limit: int = Query(20, ge=1, le=100)) -> Any:
        from .categorization_queries import merchant_search
        try:
            return merchant_search(database, generation=generation, query=q, limit=limit)
        except FinCLError as exc:
            if str(exc).startswith('STALE_GENERATION'):
                raise HTTPException(409, str(exc)) from exc
            raise

    @app.get('/api/v1/merchants/similar')
    def merchants_similar_route(generation: str, merchant_key: str) -> Any:
        from .categorization_queries import merchant_similar
        try:
            return merchant_similar(database, generation=generation, merchant_key=merchant_key)
        except FinCLError as exc:
            if str(exc).startswith('STALE_GENERATION'):
                raise HTTPException(409, str(exc)) from exc
            if str(exc) == 'Unknown merchant identity':
                raise HTTPException(404, str(exc)) from exc
            raise

    @app.post('/api/v1/categorization/merchant-rename/preview')
    def categorization_merchant_rename_preview(draft: MerchantRenameDraft) -> Any:
        from .categorization_queries import merchant_rename_preview
        try:
            return merchant_rename_preview(
                database, generation=draft.generation, merchant_key=draft.merchant_key,
                display_name=draft.display_name, reason=draft.reason,
            )
        except FinCLError as exc:
            if str(exc).startswith('STALE_GENERATION'):
                raise HTTPException(409, str(exc)) from exc
            raise

    @app.post('/api/v1/categorization/impact')
    def categorization_impact(draft: CategorizationImpactDraft) -> Any:
        """Exact pure preview for a rule alone or an alias-plus-rule action."""
        from .categorization import canonical_match, rule_id_for, validate_alias_patterns
        from .engine import preview_rule_impact
        connection = connect_readonly(database)
        try:
            current = current_generation(connection)
            if current is None or current['generation_token'] != draft.generation:
                raise HTTPException(409, 'STALE_GENERATION: reload and preview again. Your draft is preserved.')
            canonical = canonical_match(draft.match)
            alias = None
            if draft.alias_canonical is not None or draft.alias_patterns is not None:
                if not draft.alias_canonical or draft.alias_patterns is None:
                    raise FinCLError('An alias preview needs both canonical name and patterns')
                alias = (draft.alias_canonical, validate_alias_patterns(draft.alias_patterns))
            group_name, category = draft.category.split(' / ', 1)
            impact = preview_rule_impact(
                connection, rule_id=rule_id_for(canonical), match=canonical,
                group_name=group_name, category=category, proposed_alias=alias,
            )
            return {'generation': draft.generation, 'impact': impact}
        finally:
            connection.close()

    @app.post('/api/v1/rules/preview')
    def rules_preview(command: Command) -> Any:
        if command.kind not in {'CATEGORY_RULE_SET','CATEGORY_RULE_RETIRE','MERCHANT_ALIAS_SET','MERCHANT_ALIAS_RETIRE'}:
            raise FinCLError('Choose a rule or merchant-alias command')
        with ledger_writer(database):
            _validate(database,command)
        return {'valid':True,'generation':command.expected_generation,'effect':command.payload}

    @app.post('/api/v1/rules/compile')
    def rules_compile(draft: RuleDraft) -> Any:
        # Server owns rule_id_for(); the browser cannot compute the anchor hash.
        # Unlike the former SQL literal count, this dry-run uses the same
        # precedence evaluator as categorization and never writes a projection.
        from .categorization import canonical_match, rule_id_for
        from .custom_categories import custom_categories
        from .engine import preview_rule_impact
        canonical = canonical_match(draft.match)
        rule_id = rule_id_for(canonical)
        resolve_category(draft.category, custom_categories(database))
        connection = connect_readonly(database)
        try:
            group_name, category = draft.category.split(' / ', 1)
            impact = preview_rule_impact(
                connection, rule_id=rule_id, match=canonical,
                group_name=group_name, category=category,
            )
        finally:
            connection.close()
        return {
            'command': {'kind':'CATEGORY_RULE_SET','anchors':{'rule_id':rule_id},
                        'payload':{'match':canonical,'category':draft.category,'reason':draft.reason}},
            # Compatibility for the current rule builder; consumers should use
            # impact to distinguish literal matches from effective assignments.
            'match_count': impact['predicate_matches'],
            'impact': impact,
        }

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
                     direction: str = '', behavior: str = '', need: str = '', basis: str = '',
                     unresolved_only: bool = False,
                     include_excluded: bool = False, sort: str = 'date',
                     cursor: str | None = None, limit: int = 50) -> Any:
        with ledger_writer(database):
            try:
                return transactions_page(
                    database, generation=generation, start=start, end=end, currency=currency,
                    search=search, category=category, account=account, kind=kind,
                    direction=direction, behavior=behavior, need=need, basis=basis,
                    unresolved_only=unresolved_only,
                    include_excluded=include_excluded, sort=sort, cursor=cursor, limit=limit,
                )
            except FinCLError as exc:
                if str(exc).startswith('STALE_GENERATION'):
                    raise HTTPException(409, str(exc)) from exc
                raise

    @app.get('/api/v1/transactions/entry',response_model=TransactionEntry)
    def transaction_entry_route(anchor: str) -> Any:
        from .queries import transaction_entry
        with ledger_writer(database):
            try:
                return transaction_entry(database, anchor)
            except FinCLError as exc:
                raise HTTPException(404, str(exc)) from exc

    @app.get('/api/v1/transactions/anchors')
    def transaction_anchors_route(generation: str, start: str, end: str, currency: str = 'SGD',
                                  search: str = '', category: str = '', account: str = '', kind: str = '',
                                  direction: str = '', behavior: str = '', need: str = '', basis: str = '',
                                  unresolved_only: bool = False,
                                  include_excluded: bool = False) -> Any:
        from .queries import transaction_anchors
        with ledger_writer(database):
            try:
                return transaction_anchors(
                    database, generation=generation, start=start, end=end, currency=currency,
                    search=search, category=category, account=account, kind=kind,
                    direction=direction, behavior=behavior, need=need, basis=basis,
                    unresolved_only=unresolved_only,
                    include_excluded=include_excluded,
                )
            except FinCLError as exc:
                if str(exc).startswith('STALE_GENERATION'):
                    raise HTTPException(409, str(exc)) from exc
                raise

    @app.get('/api/v1/inflows/candidates')
    def inflow_candidates() -> Any:
        """Grouped, review-only inbound candidates; no receipt is auto-confirmed."""
        from .planning.queries import recurring_page
        with ledger_writer(database):
            page = recurring_page(database)
            return {
                'generation': page['generation'],
                'candidates': page['income_candidates'],
                'exclusions': page['income_exclusions'],
                'policy': 'Candidates are Likely only. Confirming a classification remains an explicit journal decision.',
            }

    @app.get('/api/v1/inflows/rules')
    def inbound_rules_list() -> Any:
        """Active reusable inbound-classification rules, newest first."""
        connection = connect_readonly(database)
        try:
            generation = current_generation(connection)
            rows = connection.execute(
                "SELECT rule_id, match_json, classification, reason, journal_seq FROM inbound_rules ORDER BY journal_seq DESC, rule_id"
            ).fetchall()
            return {
                'generation': generation['generation_token'] if generation else None,
                'rules': [{**dict(row), 'match': json.loads(row['match_json'])} for row in rows],
            }
        finally:
            connection.close()

    @app.post('/api/v1/inflows/rules/compile')
    def inbound_rule_compile(body: InboundRuleDraft) -> Any:
        """Resolve a predicate to its rule id and count the inflows it would match."""
        from .categorization import canonical_match, rule_id_for
        from .financial_roles import _matches
        from .inbound_rules import validate_inbound_rule
        with ledger_writer(database):
            connection = connect_readonly(database)
            try:
                generation = current_generation(connection)
                if generation is None or generation['generation_token'] != body.generation:
                    raise HTTPException(409, 'STALE_GENERATION: reload and preview again.')
                match = canonical_match(body.match)
                rule_id = rule_id_for(match)
                payload = {'match': match, 'classification': body.classification, 'reason': body.reason}
                validate_inbound_rule('INBOUND_RULE_SET', {'rule_id': rule_id}, payload)
                from .queries import _ENTRY_VIEW
                matched = pending = 0
                for row in connection.execute(f'SELECT * FROM ({_ENTRY_VIEW})').fetchall():
                    raw = dict(row)
                    if raw['economic_direction'] != 'IN':
                        continue
                    entry = {**raw, 'account_id': raw['role_account_id'], 'description_text': raw['role_description_text'],
                             'paynow_to': raw['role_paynow_to'], 'merchant_match': raw['role_merchant_match']}
                    if _matches(entry, match):
                        matched += 1
                        if raw['bucket'] == 'pending_review':
                            pending += 1
                return {
                    'command': {'kind': 'INBOUND_RULE_SET', 'anchors': {'rule_id': rule_id}, 'payload': payload},
                    'impact': {'inbound_matches': matched, 'still_unresolved': pending},
                }
            finally:
                connection.close()

    @app.post('/api/v1/financial-roles/impact')
    def financial_role_impact(body: FinancialRoleImpactDraft) -> Any:
        """Exact, non-mutating bulk-role preview for a frozen anchor selection."""
        from .financial_roles import validate_financial_role
        with ledger_writer(database):
            connection = connect_readonly(database)
            try:
                generation = current_generation(connection)
                if generation is None or generation['generation_token'] != body.generation:
                    raise HTTPException(409, 'STALE_GENERATION: reload and preview again.')
                if body.category is None and body.cost_behavior is None and body.need is None and not body.clear_category and not body.clear_roles:
                    raise FinCLError('Choose at least one field to change')
                if body.category is not None:
                    resolve_category(body.category, custom_categories(database))
                if body.cost_behavior is not None or body.need is not None:
                    validate_financial_role('ENTRY_ROLE_SET', {'entry': body.anchors[0]}, {'cost_behavior': body.cost_behavior, 'need': body.need, 'reason': 'preview'})
                marks = ','.join('?' for _ in body.anchors)
                from .queries import _ENTRY_VIEW, _shape
                rows = connection.execute(
                    f"SELECT * FROM ({_ENTRY_VIEW}) WHERE natural_anchor IN ({marks})",
                    tuple(body.anchors),
                ).fetchall()
                if len(rows) != len(body.anchors):
                    raise FinCLError('One or more selected transactions are no longer active')
                rules = [dict(rule) for rule in connection.execute('SELECT * FROM role_rules')]
                by_currency: dict[str, list[dict[str, Any]]] = {}
                for row in rows:
                    item = _shape(row, rules)
                    if item['lifecycle_state'] != 'COMPLETED' or not item['eligible']:
                        raise FinCLError('One or more selected transactions are inactive')
                    if (body.cost_behavior is not None or body.need is not None or body.clear_roles) and item['bucket'] != 'spending':
                        raise FinCLError('Behavior and need apply only to external outflows; remove incompatible transactions')
                    by_currency.setdefault(item['currency'], []).append(item)
                return {
                    'will_change': sum(
                        body.category is not None and r['category'] != body.category
                        or body.clear_category and r['basis'] == 'manual'
                        or body.cost_behavior is not None and r['cost_behavior'] != body.cost_behavior
                        or body.need is not None and r['need'] != body.need
                        or body.clear_roles and (r['cost_behavior_basis'] == 'manual' or r['need_basis'] == 'manual')
                        for values in by_currency.values() for r in values
                    ), 'by_currency': [
                        {'currency': currency, 'count': len(values),
                         'absolute_amount': format_decimal(*exact_sum(*[
                             _absolute_money(r['amount']) for r in values
                         ])),
                         'category_changed': sum((body.category is not None and r['category'] != body.category) or (body.clear_category and r['basis'] == 'manual') for r in values),
                         'behavior_changed': sum((body.cost_behavior is not None and r['cost_behavior'] != body.cost_behavior) or (body.clear_roles and r['cost_behavior_basis'] == 'manual') for r in values),
                         'need_changed': sum((body.need is not None and r['need'] != body.need) or (body.clear_roles and r['need_basis'] == 'manual') for r in values)}
                        for currency, values in sorted(by_currency.items())
                    ],
                    'protected_overrides': 0,
                    'cash_flow_effect': 'spending total unchanged; reserve eligibility may improve',
                }
            finally:
                connection.close()

    @app.post('/api/v1/classification/preview')
    def classification_preview_route(draft: ClassificationDraftIn) -> Any:
        """Exact, non-mutating combined preview for the unified classification
        draft (category, spending pattern/need, incoming type together).

        Returns the exact ``commands`` a caller submits verbatim via the
        existing ``POST /api/v1/batch`` to commit this draft; preview and
        commit therefore cannot disagree about what a draft implies."""
        from .classification import ClassificationDraft, FieldEdit
        from .classification import evaluate_classification_draft as _evaluate

        def _edit(raw: FieldEditIn | None) -> FieldEdit:
            return FieldEdit(raw.intent, raw.value) if raw is not None else FieldEdit()

        with ledger_writer(database):
            connection = connect_readonly(database)
            try:
                generation = current_generation(connection)
                if generation is None or generation['generation_token'] != draft.generation:
                    raise HTTPException(409, 'STALE_GENERATION: reload and preview again. Your draft is preserved.')
                internal = ClassificationDraft(
                    anchors=tuple(draft.anchors),
                    category=_edit(draft.category), cost_behavior=_edit(draft.cost_behavior),
                    need=_edit(draft.need), incoming_type=_edit(draft.incoming_type),
                    clear_category=draft.clear_category, clear_roles=draft.clear_roles,
                    policy=draft.policy, reason=draft.reason,
                )
                try:
                    result = _evaluate(connection, internal, custom_categories=frozenset(custom_categories(database)))
                except FinCLError as exc:
                    raise HTTPException(422, str(exc)) from exc
                return {'generation': draft.generation, **result}
            finally:
                connection.close()

    @app.post('/api/v1/financial-roles/rules/compile')
    def financial_role_rule_compile(body: RoleRuleDraft) -> Any:
        """Preview a reusable role rule using the same precedence evaluator."""
        from .categorization import canonical_match, rule_id_for
        from .financial_roles import _matches, validate_financial_role
        with ledger_writer(database):
            connection = connect_readonly(database)
            try:
                generation = current_generation(connection)
                if generation is None or generation['generation_token'] != body.generation:
                    raise HTTPException(409, 'STALE_GENERATION: reload and preview again.')
                match = canonical_match(body.match)
                rule_id = rule_id_for(match)
                payload = {'match': match, 'cost_behavior': body.cost_behavior, 'need': body.need, 'reason': body.reason}
                validate_financial_role('ROLE_RULE_SET', {'rule_id': rule_id}, payload)
                from .queries import _ENTRY_VIEW, _shape
                rules = [dict(rule) for rule in connection.execute('SELECT * FROM role_rules')]
                entries = []
                for row in connection.execute(f'SELECT * FROM ({_ENTRY_VIEW})').fetchall():
                    raw = dict(row)
                    entry = _shape(row, rules)
                    entries.append((entry, {**raw, 'account_id': raw['role_account_id'], 'description_text': raw['role_description_text'], 'paynow_to': raw['role_paynow_to'], 'merchant_match': raw['role_merchant_match']}))
                matched = [entry for entry, raw in entries if entry['bucket'] == 'spending' and _matches(raw, match)]
                protected = sum(1 for entry in matched if entry['cost_behavior_basis'] == 'manual' or entry['need_basis'] == 'manual')
                changed = sum(1 for entry in matched if not ((body.cost_behavior is None or entry['cost_behavior'] == body.cost_behavior) and (body.need is None or entry['need'] == body.need)))
                return {'command': {'kind': 'ROLE_RULE_SET', 'anchors': {'rule_id': rule_id}, 'payload': payload}, 'impact': {'matches': len(matched), 'will_change': changed - protected, 'protected_overrides': protected}}
            finally:
                connection.close()

    @app.get('/api/v1/financial-roles/rules')
    def financial_role_rules() -> Any:
        """Active reusable role rules, kept separate from category rules."""
        connection = connect_readonly(database)
        try:
            generation = current_generation(connection)
            rows = connection.execute(
                "SELECT rule_id, match_json, cost_behavior, need, reason, journal_seq FROM role_rules ORDER BY journal_seq DESC, rule_id"
            ).fetchall()
            return {
                'generation': generation['generation_token'] if generation else None,
                'rules': [{**dict(row), 'match': json.loads(row['match_json'])} for row in rows],
            }
        finally:
            connection.close()

    @app.get('/api/v1/rules/grouped')
    def rules_grouped(generation: str) -> Any:
        """One row per ``rule_id``, joining category/role/inbound rules that
        share a match predicate — the read side of the combined rule form."""
        from .v05 import grouped_rules_page
        page = grouped_rules_page(database)
        if page['generation'] != generation:
            raise HTTPException(409, 'STALE_GENERATION: reload and preview again.')
        return page

    @app.post('/api/v1/rules/compile-combined')
    def rules_compile_combined(draft: CombinedRuleDraft) -> Any:
        """Preview 1-3 rule commands (category/role/inbound) that share one
        rule_id, reusing each kind's own compiler and precedence evaluator so
        the combined form never reimplements precedence."""
        from .categorization import canonical_match, rule_id_for
        from .engine import preview_rule_impact
        from .financial_roles import _matches, validate_financial_role
        from .inbound_rules import validate_inbound_rule
        from .queries import _ENTRY_VIEW, _shape
        if draft.category is None and draft.cost_behavior is None and draft.need is None and draft.incoming_type is None:
            raise FinCLError('Choose at least one field to set')
        with ledger_writer(database):
            connection = connect_readonly(database)
            try:
                generation = current_generation(connection)
                if generation is None or generation['generation_token'] != draft.generation:
                    raise HTTPException(409, 'STALE_GENERATION: reload and preview again. Your draft is preserved.')
                canonical = canonical_match(draft.match)
                rule_id = rule_id_for(canonical)
                existing_category = connection.execute('SELECT 1 FROM category_rules WHERE rule_id=?', (rule_id,)).fetchone() is not None
                existing_role = connection.execute('SELECT 1 FROM role_rules WHERE rule_id=?', (rule_id,)).fetchone() is not None
                existing_inbound = connection.execute('SELECT 1 FROM inbound_rules WHERE rule_id=?', (rule_id,)).fetchone() is not None

                commands: list[dict[str, Any]] = []
                impact: dict[str, Any] = {}

                if draft.category is not None:
                    resolve_category(draft.category, custom_categories(database))
                    group_name, category = draft.category.split(' / ', 1)
                    impact['category'] = preview_rule_impact(
                        connection, rule_id=rule_id, match=canonical, group_name=group_name, category=category,
                    )
                    commands.append({'kind': 'CATEGORY_RULE_SET', 'anchors': {'rule_id': rule_id},
                                      'payload': {'match': canonical, 'category': draft.category, 'reason': draft.reason}})
                elif existing_category:
                    commands.append({'kind': 'CATEGORY_RULE_RETIRE', 'anchors': {'rule_id': rule_id}, 'payload': {'reason': draft.reason}})

                if draft.cost_behavior is not None or draft.need is not None:
                    payload = {'match': canonical, 'cost_behavior': draft.cost_behavior, 'need': draft.need, 'reason': draft.reason}
                    validate_financial_role('ROLE_RULE_SET', {'rule_id': rule_id}, payload)
                    role_rules = [dict(rule) for rule in connection.execute('SELECT * FROM role_rules')]
                    matched = []
                    for row in connection.execute(f'SELECT * FROM ({_ENTRY_VIEW})').fetchall():
                        raw = dict(row)
                        entry = _shape(row, role_rules)
                        raw_match = {**raw, 'account_id': raw['role_account_id'], 'description_text': raw['role_description_text'],
                                     'paynow_to': raw['role_paynow_to'], 'merchant_match': raw['role_merchant_match']}
                        if entry['bucket'] == 'spending' and _matches(raw_match, canonical):
                            matched.append(entry)
                    protected = sum(1 for entry in matched if entry['cost_behavior_basis'] == 'manual' or entry['need_basis'] == 'manual')
                    changed = sum(
                        1 for entry in matched
                        if not ((draft.cost_behavior is None or entry['cost_behavior'] == draft.cost_behavior)
                                and (draft.need is None or entry['need'] == draft.need))
                    )
                    commands.append({'kind': 'ROLE_RULE_SET', 'anchors': {'rule_id': rule_id}, 'payload': payload})
                    impact['role'] = {'matches': len(matched), 'will_change': changed - protected, 'protected_overrides': protected}
                elif existing_role:
                    commands.append({'kind': 'ROLE_RULE_RETIRE', 'anchors': {'rule_id': rule_id}, 'payload': {'reason': draft.reason}})

                if draft.incoming_type is not None:
                    payload = {'match': canonical, 'classification': draft.incoming_type, 'reason': draft.reason}
                    validate_inbound_rule('INBOUND_RULE_SET', {'rule_id': rule_id}, payload)
                    inbound_matches = still_unresolved = 0
                    for entry in _generic_entries(connection):
                        if entry['economic_direction'] != 'IN':
                            continue
                        if _matches(entry, canonical):
                            inbound_matches += 1
                            if entry['bucket'] == 'pending_review':
                                still_unresolved += 1
                    commands.append({'kind': 'INBOUND_RULE_SET', 'anchors': {'rule_id': rule_id}, 'payload': payload})
                    impact['incoming_type'] = {'inbound_matches': inbound_matches, 'still_unresolved': still_unresolved}
                elif existing_inbound:
                    commands.append({'kind': 'INBOUND_RULE_RETIRE', 'anchors': {'rule_id': rule_id}, 'payload': {'reason': draft.reason}})

                if not commands:
                    raise FinCLError('Choose at least one field to change')
                return {'rule_id': rule_id, 'commands': commands, 'impact': impact}
            finally:
                connection.close()

    @app.post('/api/v1/rules/{rule_id}/retire-combined/preview')
    def rules_retire_combined_preview(rule_id: str, draft: CombinedRetireDraft) -> Any:
        """Every ``*_RULE_RETIRE`` for the kinds a combined rule row carries."""
        from .engine import preview_rule_retirement_impact
        connection = connect_readonly(database)
        try:
            current = current_generation(connection)
            if current is None or current['generation_token'] != draft.generation:
                raise HTTPException(409, 'STALE_GENERATION: reload and preview again.')
            existing_category = connection.execute('SELECT 1 FROM category_rules WHERE rule_id=?', (rule_id,)).fetchone() is not None
            existing_role_row = connection.execute('SELECT match_json FROM role_rules WHERE rule_id=?', (rule_id,)).fetchone()
            existing_inbound_row = connection.execute('SELECT match_json FROM inbound_rules WHERE rule_id=?', (rule_id,)).fetchone()
            if not (existing_category or existing_role_row or existing_inbound_row):
                raise HTTPException(404, 'Rule not found')
            from .financial_roles import _matches

            commands: list[dict[str, Any]] = []
            impact: dict[str, Any] = {}
            if existing_category:
                commands.append({'kind': 'CATEGORY_RULE_RETIRE', 'anchors': {'rule_id': rule_id}, 'payload': {'reason': draft.reason}})
                impact['category'] = preview_rule_retirement_impact(connection, rule_id=rule_id)
            if existing_role_row is not None:
                match = json.loads(existing_role_row['match_json'])
                entries = _generic_entries(connection)
                matched = sum(1 for entry in entries if entry['bucket'] == 'spending' and _matches(entry, match))
                commands.append({'kind': 'ROLE_RULE_RETIRE', 'anchors': {'rule_id': rule_id}, 'payload': {'reason': draft.reason}})
                impact['role'] = {'matches': matched}
            if existing_inbound_row is not None:
                match = json.loads(existing_inbound_row['match_json'])
                entries = _generic_entries(connection)
                matched = sum(1 for entry in entries if entry['economic_direction'] == 'IN' and _matches(entry, match))
                commands.append({'kind': 'INBOUND_RULE_RETIRE', 'anchors': {'rule_id': rule_id}, 'payload': {'reason': draft.reason}})
                impact['incoming_type'] = {'inbound_matches': matched}
            return {'rule_id': rule_id, 'commands': commands, 'impact': impact}
        finally:
            connection.close()

    @app.get('/api/v1/analysis',response_model=Analysis)
    def analysis(
        start: str, end: str, currency: str = 'SGD',
        comparison: str | None = None,
        trend_range: str = '6',
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
                    comparison=comparison, trend_range=trend_range,
                    expected=expected_generation,
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
                # The app's own clock, not the host's: Obligations and the
                # forecast value debts at `clock.today()`, and a pinned demo
                # clock must not leave this page on a different day.
                return debts_page(database, as_of=as_of or clock.today().isoformat(), currency=currency,
                                  status=status, cursor=cursor, limit=limit,
                                  expected=generation)
            except FinCLError as exc:
                if str(exc).startswith('STALE_GENERATION'):
                    raise HTTPException(409, str(exc)) from exc
                raise HTTPException(422, str(exc)) from exc

    @app.get('/api/v1/debts/allocation-candidates')
    def debt_allocation_candidates(debt: str, event_kind: str | None = None, search: str | None = None,
                                   cursor: str | None = None, limit: int = 50,
                                   generation: str | None = None, anchor: str | None = None) -> Any:
        from .debts.queries import allocation_candidates
        with ledger_writer(database):
            try:
                return allocation_candidates(database, debt_id=debt, event_kind=event_kind, search=search,
                                             cursor=cursor, limit=limit, expected=generation, anchor=anchor)
            except FinCLError as exc:
                if str(exc).startswith('STALE_GENERATION'):
                    raise HTTPException(409, str(exc)) from exc
                raise HTTPException(404 if 'does not exist' in str(exc) else 422, str(exc)) from exc

    @app.get('/api/v1/debts/split-candidates')
    def debt_split_candidates(search: str | None = None, cursor: str | None = None, limit: int = 50,
                              generation: str | None = None) -> Any:
        from .debts.queries import split_candidates
        with ledger_writer(database):
            try:
                return split_candidates(database, search=search, cursor=cursor, limit=limit, expected=generation)
            except FinCLError as exc:
                if str(exc).startswith('STALE_GENERATION'):
                    raise HTTPException(409, str(exc)) from exc
                raise HTTPException(422, str(exc)) from exc

    @app.get('/api/v1/debts/{debt_id}')
    def debt(debt_id: str, as_of: str | None = None, generation: str | None = None) -> Any:
        from .debts.queries import debt_detail
        with ledger_writer(database):
            try:
                return debt_detail(database, debt_id, as_of=as_of or clock.today().isoformat(), expected=generation)
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
                elif command.kind == 'REPORTING_CURRENCY_SET':
                    policy = 'reporting-currency-v1'
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

    @app.get('/api/v1/maintenance/backups')
    def backup_list() -> Any:
        root = database.parent / 'backups'
        backups = sorted((p.name for p in root.iterdir() if p.is_dir()), reverse=True) if root.is_dir() else []
        return {'backups': backups}

    @app.post('/api/v1/maintenance/backup')
    def backup_create() -> Any:
        from .backup import backup_ledger
        evidence = evidence_dir or database.parent / 'evidence'
        backup_id = f"backup-{uuid4().hex}"
        output = database.parent / 'backups' / backup_id
        output.parent.mkdir(mode=0o700, exist_ok=True)
        result = backup_ledger(database, evidence, output)
        return {'backup_id': backup_id, 'journal_seq': result.journal_seq, 'evidence_objects': result.evidence_objects, 'verified': True}

    @app.post('/api/v1/maintenance/restore-verify')
    def backup_restore_verify(body: BackupActionDraft) -> Any:
        from .backup import restore_ledger, verify_backup_bundle
        if not body.backup_id:
            raise HTTPException(422, 'Choose a backup to restore and verify.')
        bundle = database.parent / 'backups' / body.backup_id
        if not bundle.is_dir():
            raise HTTPException(404, 'That backup does not exist.')
        verify_backup_bundle(bundle)
        output = database.parent / 'restore-verifications' / f"{body.backup_id}-{uuid4().hex}"
        result = restore_ledger(bundle, output)
        restored_db = connect_readonly(output / 'fincl.sqlite3')
        try:
            generation = current_generation(restored_db)
            token = generation['generation_token'] if generation is not None else None
        finally:
            restored_db.close()
            shutil.rmtree(output)
        return {'backup_id': body.backup_id, 'verified': True, 'ledger_id': result.ledger_id, 'evidence_objects': result.evidence_objects, 'generation': token}

    @app.post('/api/v1/refresh')
    def refresh() -> Any:
        refresh_ledger(database)
        return {'status':'refreshed'}

    from .api_cards import register_card_reads
    register_card_reads(app, database)
    from .api_budgets import register_planning_routes
    register_planning_routes(app, database)
    from .api_reporting import register_reporting_routes
    register_reporting_routes(app, database)
    from .reporting_workspaces import register_workspace_reports
    register_workspace_reports(app, database)

    if assets.exists():
        if (assets / 'assets').exists():
            app.mount('/assets',StaticFiles(directory=assets / 'assets'),name='assets')
        @app.get('/')
        def index() -> FileResponse:
            return FileResponse(assets / 'index.html')
    return app
