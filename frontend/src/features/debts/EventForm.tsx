import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { CategoryPicker, rememberCategory } from '@/features/classification/CategoryPicker'
import { AllocationTable, emptyAllocation } from './AllocationTable'
import { DraftEffect, RecloseControl, useDebtDraft } from './Draft'
import {
  EVENT_KINDS,
  aroundClosed,
  bankDirection,
  kindsForMovement,
  type AllocationCandidate,
  type AllocationInput,
  type DebtDetail,
  type DebtEventRead,
  type EventKindSpec,
} from './api'

function todayIso(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

/** A row the user added but never filled in: dropped rather than sent. */
function isBlank(row: AllocationInput): boolean {
  return !row.entry_anchor && !row.principal_amount.trim()
}

/** Record one PERSONAL_DEBT_EVENT_SET against an open debt, or correct one.
 *
 * `replacing` is a recorded event being corrected: the form starts from its
 * values, and saving retires it and records the corrected event in one batch.
 * On a closed debt that batch also reopens it and, unless the user says
 * otherwise, closes it again. What it claims of its sources is offered back.
 *
 * `seed` is one source transaction handed over from the classification editor:
 * the form then offers only the event kinds that transaction's direction can
 * back, and starts with it allocated in full for its still-unclaimed amount.
 *
 * An expense someone paid for me is reported as spending under its category, so
 * it is chosen from the same category list as every other transaction.
 *
 * A source-linked event's principal may be left blank: the server takes it as
 * the total of the sources and shows it, with every later balance, when the
 * event is checked. The date follows the picked sources until the user edits it. */
export function EventForm({
  detail,
  generation,
  onSaved,
  onCancel,
  seed,
  replacing,
  categories = [],
}: {
  detail: DebtDetail
  generation: string
  onSaved: (status: string) => void | Promise<void>
  onCancel: () => void
  seed?: AllocationCandidate
  replacing?: DebtEventRead
  /** The ledger's categories; `Unclassified` is not offered, since it says nothing about the expense. */
  categories?: string[]
}) {
  const debt = detail.debt
  const old = replacing?.event
  const kinds = seed ? kindsForMovement(debt.direction, seed.economic_direction) : EVENT_KINDS[debt.direction]
  const [spec, setSpec] = useState<EventKindSpec>(kinds.find((k) => k.kind === old?.event_kind) ?? kinds[0])
  // A source-linked event's principal is the total of its sources, so a
  // correction starts it blank and lets the server work it out again.
  const [principal, setPrincipal] = useState(old && !old.entry_allocations.length ? old.principal_amount : '')
  // A debt's first event usually opens it; later ones happen now-ish.
  const [effectiveOn, setEffectiveOn] = useState(() => {
    if (old) return old.effective_on
    if (seed) return seed.effective_at.slice(0, 10)
    const today = todayIso()
    return debt.event_count > 0 && today > debt.opened_on ? today : debt.opened_on
  })
  const [dateAuto, setDateAuto] = useState(!seed && !old)
  const [sourceDated, setSourceDated] = useState(false)
  const [openingBalance, setOpeningBalance] = useState(old?.opening_balance ?? false)
  const [expenseCategory, setExpenseCategory] = useState(old?.expense_category ?? '')
  const [rows, setRows] = useState<AllocationInput[]>(
    old ? old.entry_allocations
      : seed ? [{ ...emptyAllocation(seed.entry_anchor), principal_amount: seed.open_amount }] : [],
  )
  const [recloseOn, setRecloseOn] = useState<string | null>(debt.closed_on)
  const [reason, setReason] = useState('')
  const [problem, setProblem] = useState('')
  const draft = useDebtDraft(generation, async status => {
    if (spec.kind === 'EXPENSE_PAID_FOR_ME') rememberCategory(expenseCategory)
    await onSaved(status)
  })

  const offLedger = spec.offLedger || (spec.opening && openingBalance)
  const needsCategory = spec.kind === 'EXPENSE_PAID_FOR_ME'
  const filledRows = rows.filter((r) => !isBlank(r))

  function edit(change: () => void) { change(); setProblem(''); draft.invalidate() }

  function pickKind(kind: string) {
    const next = kinds.find((k) => k.kind === kind) ?? kinds[0]
    edit(() => {
      setSpec(next)
      // A seeded source suits every kind offered (they share its direction), so
      // it survives a kind change; otherwise the picked sources may not. A
      // corrected event keeps its own sources for the server to judge.
      if (!seed && !old) { setRows([]); setSourceDated(false) }
      if (!next.opening) setOpeningBalance(false)
    })
  }

  function updateRows(next: AllocationInput[], picked?: AllocationCandidate) {
    edit(() => {
      setRows(next)
      if (picked && dateAuto) {
        const day = picked.effective_at.slice(0, 10)
        const floor = day < debt.opened_on ? debt.opened_on : day
        setEffectiveOn((prev) => (sourceDated && prev > floor ? prev : floor))
        setSourceDated(true)
      }
    })
  }

  function submit() {
    if (draft.preview) { void draft.save(reason.trim()); return }
    const missing = !reason.trim() ? 'A reason is required.'
      : offLedger && !principal.trim() ? 'Enter the principal.'
      : needsCategory && !expenseCategory ? 'Choose an expense category from the list.'
      : !offLedger && !filledRows.length ? 'Pick the transaction that moved this money.'
      : !offLedger && filledRows.some((r) => !r.entry_anchor) ? 'Pick a transaction for every source row that has an amount, or remove the row.'
      : ''
    if (missing) { setProblem(missing); return }
    const retire = old ? [{ kind: 'PERSONAL_DEBT_EVENT_RETIRE', anchors: { debt: debt.debt_id, event: old.event_id }, payload: { reason: reason.trim() } }] : []
    void draft.check(aroundClosed(debt, [...retire, {
      kind: 'PERSONAL_DEBT_EVENT_SET',
      anchors: { debt: debt.debt_id, event: crypto.randomUUID() },
      payload: {
        event_kind: spec.kind,
        direction: debt.direction,
        principal_amount: principal.trim() || null,
        effective_on: effectiveOn,
        entry_allocations: offLedger ? [] : filledRows,
        opening_balance: spec.opening && openingBalance,
        expense_category: needsCategory ? expenseCategory : null,
        reason: reason.trim(),
      },
    }], { reopenOn: detail.as_of, recloseOn, reason: reason.trim() }))
  }

  const checkedPrincipal = draft.preview?.items.find((i) => i.kind === 'PERSONAL_DEBT_EVENT_SET')?.payload.principal_amount

  return (
    <div className="debt-event-form">
      <div className="debt-event-title">
        <h3>{old ? 'Correct an event' : 'Record an event'} · {debt.counterparty_label}</h3>
        <span>{debt.direction === 'PAYABLE' ? 'I owe them' : 'They owe me'}</span>
      </div>
      <div className="debt-event-grid">
        <label>
          Event
          <select
            aria-label="Event kind"
            value={spec.kind}
            onChange={(e) => pickKind(e.target.value)}
            disabled={draft.busy}
          >
            {kinds.map((k) => <option key={k.kind} value={k.kind}>{k.label}</option>)}
          </select>
        </label>
        <label>
          Principal
          <span className="debt-event-money">
            <span aria-hidden>{debt.currency}</span>
            <input
              aria-label="Event principal"
              inputMode="decimal"
              placeholder={offLedger ? '0.00' : 'Total of the sources'}
              value={principal}
              onChange={(e) => edit(() => setPrincipal(e.target.value))}
              disabled={draft.busy}
            />
          </span>
          {!offLedger && !principal.trim() ? <small>Leave blank to use the total of the sources below</small> : null}
        </label>
        <label>
          Effective date
          <input
            aria-label="Effective date"
            type="date"
            value={effectiveOn}
            min={debt.opened_on}
            onChange={(e) => edit(() => { setEffectiveOn(e.target.value); setDateAuto(false) })}
            disabled={draft.busy}
          />
          {!offLedger && dateAuto ? <small>Follows the transaction you pick</small> : null}
        </label>
        {spec.opening ? (
          <label className="debt-event-check">
            <input
              type="checkbox"
              checked={openingBalance}
              onChange={(e) => edit(() => setOpeningBalance(e.target.checked))}
              disabled={draft.busy}
            />
            <span>
              Opening balance
              <small>An amount already owed before tracking started, with no bank transaction behind it.</small>
            </span>
          </label>
        ) : null}
        {needsCategory ? (
          <CategoryPicker
            id={`debt-expense-category-${debt.debt_id}`}
            label="Expense category"
            categories={categories.filter((c) => !c.startsWith('Unclassified /'))}
            value={expenseCategory}
            onChange={(c) => edit(() => setExpenseCategory(c))}
            disabled={draft.busy}
          />
        ) : null}
      </div>

      {offLedger ? (
        <p className="debt-event-note">
          No bank transaction backs this event — it records an off-ledger amount.
        </p>
      ) : (
        <AllocationTable
          debtId={debt.debt_id}
          eventKind={spec.kind}
          generation={generation}
          currency={debt.currency}
          rows={rows.length ? rows : [emptyAllocation()]}
          onChange={updateRows}
          sourceDirection={bankDirection(debt.direction, spec)}
          known={seed ? [seed] : replacing?.sources}
          replacing={old?.event_id}
        />
      )}

      <label className="debt-event-reason">
        Reason
        <textarea
          aria-label="Reason"
          placeholder={old ? 'Why is the recorded event wrong?' : spec.sign > 0 ? 'e.g. Rent top-up for October' : 'e.g. Monthly repayment'}
          value={reason}
          onChange={(e) => edit(() => setReason(e.target.value))}
          disabled={draft.busy}
          rows={2}
        />
      </label>

      {debt.status === 'CLOSED' && debt.closed_on
        ? <RecloseControl closedOn={debt.closed_on} value={recloseOn} onChange={(v) => edit(() => setRecloseOn(v))} disabled={draft.busy} />
        : null}
      {draft.preview && typeof checkedPrincipal === 'string' && !principal.trim()
        ? <p className="muted-note">Principal: the sources total {checkedPrincipal} {debt.currency}.</p> : null}
      {draft.preview ? <DraftEffect preview={draft.preview} /> : null}
      {problem || draft.error ? <p className="debt-event-error" role="alert">{problem || draft.error}</p> : null}
      <div className="debt-event-actions">
        <Button variant="ghost" size="sm" onClick={onCancel} disabled={draft.busy}>Cancel</Button>
        <Button size="sm" onClick={submit} disabled={draft.busy}>
          {draft.busy ? 'Working…' : draft.preview ? (draft.retrying ? 'Retry save' : old ? 'Save correction' : 'Save event') : old ? 'Check correction' : 'Check event'}
        </Button>
      </div>
    </div>
  )
}
