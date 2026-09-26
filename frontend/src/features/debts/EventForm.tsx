import { useMemo, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { ApiError } from '@/lib/api'
import { CategoryPicker, rememberCategory } from '@/features/classification/CategoryPicker'
import { AllocationTable, emptyAllocation, principalTotalMinor } from './AllocationTable'
import {
  EVENT_KINDS,
  bankDirection,
  kindsForMovement,
  previewThenCommit,
  type AllocationCandidate,
  type AllocationInput,
  type DebtDetail,
  type EventKindSpec,
} from './api'

function todayIso(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

/** A row the user added but never filled in: dropped rather than sent. */
function isBlank(row: AllocationInput): boolean {
  return !row.entry_anchor && principalTotalMinor([row]) === 0
}

/** Record one PERSONAL_DEBT_EVENT_SET against an open debt.
 *
 * `seed` is one source transaction handed over from the classification editor:
 * the form then offers only the event kinds that transaction's direction can
 * back, and starts with it allocated in full for its still-unclaimed amount.
 *
 * An expense someone paid for me is reported as spending under its category, so
 * it is chosen from the same category list as every other transaction.
 *
 * The principal and the date follow the picked sources until the user edits
 * them: the principal tracks the allocated total, the date the latest source. */
export function EventForm({
  detail,
  generation,
  onSaved,
  onCancel,
  seed,
  categories = [],
}: {
  detail: DebtDetail
  generation: string
  onSaved: (status: string) => void | Promise<void>
  onCancel: () => void
  seed?: AllocationCandidate
  /** The ledger's categories; `Unclassified` is not offered, since it says nothing about the expense. */
  categories?: string[]
}) {
  const debt = detail.debt
  const kinds = seed ? kindsForMovement(debt.direction, seed.economic_direction) : EVENT_KINDS[debt.direction]
  const [spec, setSpec] = useState<EventKindSpec>(kinds[0])
  const [principal, setPrincipal] = useState(seed?.open_amount ?? '')
  const [principalAuto, setPrincipalAuto] = useState(true)
  // A debt's first event usually opens it; later ones happen now-ish.
  const [effectiveOn, setEffectiveOn] = useState(() => {
    if (seed) return seed.effective_at.slice(0, 10)
    const today = todayIso()
    return debt.event_count > 0 && today > debt.opened_on ? today : debt.opened_on
  })
  const [dateAuto, setDateAuto] = useState(!seed)
  const sourceDated = useRef(false)
  const [openingBalance, setOpeningBalance] = useState(false)
  const [expenseCategory, setExpenseCategory] = useState('')
  const [rows, setRows] = useState<AllocationInput[]>(
    seed ? [{ ...emptyAllocation(seed.entry_anchor), principal_amount: seed.open_amount }] : [],
  )
  const [reason, setReason] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const inFlight = useRef(false)

  const offLedger = spec.offLedger || (spec.opening && openingBalance)
  const needsCategory = spec.kind === 'EXPENSE_PAID_FOR_ME'

  const filledRows = useMemo(() => rows.filter((r) => !isBlank(r)), [rows])
  const principalBalanced = useMemo(() => {
    if (offLedger) return true
    const want = Math.round(Number(principal) * 100)
    return Number.isFinite(want) && principalTotalMinor(filledRows) === want && filledRows.length > 0
  }, [offLedger, principal, filledRows])

  function pickKind(kind: string) {
    const next = kinds.find((k) => k.kind === kind) ?? kinds[0]
    setSpec(next)
    // A seeded source suits every kind offered (they share its direction), so
    // it survives a kind change; otherwise the picked sources may not.
    if (!seed) {
      setRows([])
      sourceDated.current = false
      if (principalAuto) setPrincipal('')
    }
    if (!next.opening) setOpeningBalance(false)
  }

  // The principal follows the allocated total as sources are picked, added and
  // edited -- until the user types their own: the stated principal is the claim
  // the allocations are checked against, so once typed it is theirs to change.
  function updateRows(next: AllocationInput[], picked?: AllocationCandidate) {
    setRows(next)
    if (principalAuto) {
      const total = principalTotalMinor(next)
      if (Number.isFinite(total)) setPrincipal(total > 0 ? (total / 100).toFixed(2) : '')
    }
    if (picked && dateAuto) {
      const day = picked.effective_at.slice(0, 10)
      const floor = day < debt.opened_on ? debt.opened_on : day
      setEffectiveOn((prev) => (sourceDated.current && prev > floor ? prev : floor))
      sourceDated.current = true
    }
  }

  function editPrincipal(value: string) {
    setPrincipal(value)
    setPrincipalAuto(!value.trim())
  }

  async function submit() {
    if (inFlight.current) return
    if (!reason.trim() || !principal.trim() || (needsCategory && !expenseCategory)) {
      setError('Principal, a reason' + (needsCategory ? ' and an expense category from the list' : '') + ' are required.')
      return
    }
    if (!offLedger && filledRows.some((r) => !r.entry_anchor)) {
      setError('Pick a transaction for every source row that has an amount, or remove the row.')
      return
    }
    if (!principalBalanced) {
      setError('The source allocations must sum to the event principal.')
      return
    }
    inFlight.current = true; setBusy(true); setError('')
    const body = {
      request_id: crypto.randomUUID(),
      expected_generation: generation,
      kind: 'PERSONAL_DEBT_EVENT_SET',
      anchors: { debt: debt.debt_id, event: crypto.randomUUID() },
      payload: {
        event_kind: spec.kind,
        direction: debt.direction,
        principal_amount: principal.trim(),
        effective_on: effectiveOn,
        entry_allocations: offLedger ? [] : filledRows,
        opening_balance: spec.opening && openingBalance,
        expense_category: needsCategory ? expenseCategory : null,
        reason: reason.trim(),
      },
    }
    try {
      const result = await previewThenCommit(body)
      if (needsCategory) rememberCategory(expenseCategory)
      await onSaved(result.status)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not record the event.')
    } finally {
      inFlight.current = false; setBusy(false)
    }
  }

  return (
    <div className="debt-event-form">
      <div className="debt-event-title">
        <h3>Record an event · {debt.counterparty_label}</h3>
        <span>{debt.direction === 'PAYABLE' ? 'I owe them' : 'They owe me'}</span>
      </div>
      <div className="debt-event-grid">
        <label>
          Event
          <select
            aria-label="Event kind"
            value={spec.kind}
            onChange={(e) => pickKind(e.target.value)}
            disabled={busy}
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
              placeholder="0.00"
              value={principal}
              onChange={(e) => editPrincipal(e.target.value)}
              disabled={busy}
            />
          </span>
          {!offLedger && principalAuto ? <small>Totals the sources below</small> : null}
        </label>
        <label>
          Effective date
          <input
            aria-label="Effective date"
            type="date"
            value={effectiveOn}
            min={debt.opened_on}
            onChange={(e) => { setEffectiveOn(e.target.value); setDateAuto(false) }}
            disabled={busy}
          />
          {!offLedger && dateAuto ? <small>Follows the transaction you pick</small> : null}
        </label>
        {spec.opening ? (
          <label className="debt-event-check">
            <input
              type="checkbox"
              checked={openingBalance}
              onChange={(e) => setOpeningBalance(e.target.checked)}
              disabled={busy}
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
            onChange={setExpenseCategory}
            disabled={busy}
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
          eventPrincipal={principal || '0'}
          sourceDirection={bankDirection(debt.direction, spec)}
          onUseTotal={(total) => { setPrincipal(total); setPrincipalAuto(true) }}
          known={seed ? [seed] : undefined}
        />
      )}

      <label className="debt-event-reason">
        Reason
        <textarea
          aria-label="Reason"
          placeholder={spec.sign > 0 ? 'e.g. Rent top-up for October' : 'e.g. Monthly repayment'}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          disabled={busy}
          rows={2}
        />
      </label>

      {error ? <p className="debt-event-error" role="alert">{error}</p> : null}
      <div className="debt-event-actions">
        <Button variant="ghost" size="sm" onClick={onCancel} disabled={busy}>Cancel</Button>
        <Button size="sm" onClick={submit} disabled={busy}>
          {busy ? 'Saving…' : 'Preview and save'}
        </Button>
      </div>
    </div>
  )
}
