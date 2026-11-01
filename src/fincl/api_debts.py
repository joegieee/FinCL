"""The debt read and preview responses, as the frontend's types are generated from them."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

DebtDirection = Literal['PAYABLE', 'RECEIVABLE']
BankDirection = Literal['IN', 'OUT']


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class WireMoney(_StrictModel):
    value: str
    currency: str | None


class DebtLastEvent(_StrictModel):
    kind: str
    effective_on: str


class DebtPlanHeadline(_StrictModel):
    next_due: str | None
    overdue_count: int
    unscheduled_remainder: bool


class DebtCard(_StrictModel):
    debt_id: str
    direction: DebtDirection
    currency: str
    counterparty_label: str
    opened_on: str
    due_on: str | None
    status: Literal['OPEN', 'CLOSED']
    closed_on: str | None
    remaining_principal: WireMoney
    last_event: DebtLastEvent | None
    reconciliation_status: str
    event_count: int
    plan: DebtPlanHeadline | None


class DebtsPage(_StrictModel):
    generation: str
    as_of: str
    i_owe: list[DebtCard]
    owed_to_me: list[DebtCard]
    next_cursor: str | None


class DebtEvent(_StrictModel):
    event_id: str
    event_kind: str
    effective_on: str
    opening_balance: bool
    signed: int
    principal: WireMoney
    expense_category: str | None
    bound: bool
    dangling_reason: str | None
    reason: str
    journal_seq: int


class OverlappingCommitment(_StrictModel):
    commitment: str
    name: str
    payments: int


class PlanRow(_StrictModel):
    schedule_index: int
    due_on: str
    amount: str
    paid: str
    remaining: str
    trimmed: str
    was_trimmed: bool
    state: Literal['paid', 'part_paid', 'overdue', 'upcoming', 'trimmed']
    overdue: bool


class UnscheduledRemainder(_StrictModel):
    amount: str
    due_on: str | None
    overdue: bool


class PlanState(_StrictModel):
    effective_from: str
    plan_kind: Literal['rows', 'series']
    reason: str
    rows: list[PlanRow]
    remaining_principal: str
    scheduled_remaining: str
    unscheduled_remainder: UnscheduledRemainder | None
    next_due: str | None
    overdue_count: int


class DebtDetailCard(DebtCard):
    principal_history: list[DebtEvent]
    reason: str
    overlapping_commitments: list[OverlappingCommitment]
    schedule: PlanState | None


class DebtDetail(_StrictModel):
    generation: str
    as_of: str
    debt: DebtDetailCard


class DebtEventSummary(_StrictModel):
    event_id: str
    event_kind: str
    effective_on: str
    opening_balance: bool
    principal: WireMoney
    bound: bool
    reason: str


class DebtEventsPage(_StrictModel):
    generation: str
    events: list[DebtEventSummary]
    next_cursor: str | None


class AllocationCandidate(_StrictModel):
    entry_anchor: str
    effective_at: str
    economic_direction: BankDirection
    currency: str
    amount: str
    open_amount: str
    description: str | None
    incoming_type: str | None


class AllocationCandidatesPage(_StrictModel):
    generation: str
    debt_id: str
    event_kind: str
    required_direction: BankDirection
    currency: str
    candidates: list[AllocationCandidate]
    next_cursor: str | None


class SplitCandidatesPage(_StrictModel):
    generation: str
    candidates: list[AllocationCandidate]
    next_cursor: str | None


class OwnerShare(_StrictModel):
    expense_amount: str
    expense_category: str
    reason: str


class EntryResidual(_StrictModel):
    generation: str
    entry_anchor: str
    currency: str
    amount: str
    available_amount: str
    unassigned_amount: str
    owner_share: OwnerShare | None


class EntryAllocation(_StrictModel):
    entry_anchor: str
    principal_amount: str
    interest_amount: str
    fee_amount: str
    separate_fee_amount: str


class DebtEventPayload(_StrictModel):
    event_id: str
    event_kind: str
    direction: DebtDirection
    principal_amount: str
    effective_on: str
    entry_allocations: list[EntryAllocation]
    opening_balance: bool
    expense_category: str | None
    reason: str


class DebtEventRead(_StrictModel):
    generation: str
    debt_id: str
    event: DebtEventPayload
    sources: list[AllocationCandidate]


class DraftItem(_StrictModel):
    kind: str
    anchors: dict[str, str]
    payload: dict[str, Any]


class CommitmentScheduleDraft(_StrictModel):
    generation: str
    items: list[DraftItem]


class TimelineEvent(_StrictModel):
    event_id: str
    event_kind: str
    effective_on: str
    signed: int
    principal: str
    balance: str


class DraftTimelineEvent(TimelineEvent):
    change: Literal['added', 'changed', 'balance'] | None
    balance_before: str | None


class DraftDebtStanding(_StrictModel):
    status: str
    closed_on: str | None
    remaining: str


class DraftSchedule(_StrictModel):
    before: PlanState | None
    after: PlanState | None


class DraftDebt(_StrictModel):
    debt_id: str
    counterparty_label: str
    direction: DebtDirection
    currency: str
    before: DraftDebtStanding | None
    after: DraftDebtStanding
    events: list[DraftTimelineEvent]
    schedule: DraftSchedule
    removed: list[TimelineEvent]


class DraftPreview(_StrictModel):
    generation: str
    items: list[DraftItem]
    debts: list[DraftDebt]
