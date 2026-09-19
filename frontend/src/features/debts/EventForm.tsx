import { useMemo, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { ApiError } from '@/lib/api'
import { AllocationTable, emptyAllocation, principalTotalMinor } from './AllocationTable'
import {
  EVENT_KINDS,
  kindsForMovement,
  previewThenCommit,
  type AllocationCandidate,
  type AllocationInput,
  type DebtDetail,
  type EventKindSpec,
} from './api'

/** Record one PERSONAL_DEBT_EVENT_SET against an open debt.
 *
 * `seed` is one source transaction handed over from the classification editor:
 * the form then offers only the event kinds that transaction's direction can
 * back, and starts with it allocated in full for its still-unclaimed amount. */
export function EventForm({
  detail,
  generation,
  onSaved,
  onCancel,
  seed,
}: {
  detail: DebtDetail
  generation: string
  onSaved: (status: string) => void | Promise<void>
  onCancel: () => void
  seed?: AllocationCandidate
}) {
  const debt = detail.debt
  const kinds = seed ? kindsForMovement(debt.direction, seed.economic_direction) : EVENT_KINDS[debt.direction]
  const [spec, setSpec] = useState<EventKindSpec>(kinds[0])
  const [principal, setPrincipal] = useState(seed?.open_amount ?? '')
  const [effectiveOn, setEffectiveOn] = useState(seed ? seed.effective_at.slice(0, 10) : debt.opened_on)
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

  const principalBalanced = useMemo(() => {
    if (offLedger) return true
    const want = Math.round(Number(principal) * 100)
    return Number.isFinite(want) && principalTotalMinor(rows) === want && rows.length > 0
  }, [offLedger, principal, rows])

  function pickKind(kind: string) {
    const next = kinds.find((k) => k.kind === kind) ?? kinds[0]
    setSpec(next)
    // A seeded source suits every kind offered (they share its direction), so
    // it survives a kind change; otherwise the picked sources may not.
    if (!seed) setRows([])
    if (!next.opening) setOpeningBalance(false)
  }

  // Seeds the principal from the first source picked, but never overwrites a
  // number already there: the stated principal is the claim the allocations are
  // checked against, so once it exists it stays the user's to change.
  function updateRows(next: AllocationInput[]) {
    setRows(next)
    if (principal.trim()) return
    const total = principalTotalMinor(next)
    if (Number.isFinite(total) && total > 0) setPrincipal((total / 100).toFixed(2))
  }

  async function submit() {
    if (inFlight.current) return
    if (!reason.trim() || !principal.trim() || (needsCategory && !expenseCategory.trim())) {
      setError('Principal, a reason' + (needsCategory ? ' and an expense category' : '') + ' are required.')
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
        entry_allocations: offLedger ? [] : rows,
        opening_balance: spec.opening && openingBalance,
        expense_category: needsCategory ? expenseCategory.trim() : null,
        reason: reason.trim(),
      },
    }
    try {
      const result = await previewThenCommit(body)
      await onSaved(result.status)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not record the event.')
    } finally {
      inFlight.current = false; setBusy(false)
    }
  }

  return (
    <div className="debt-event-form">
      <h3>Record an event · {debt.counterparty_label}</h3>
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
          Principal ({debt.currency})
          <input
            aria-label="Event principal"
            inputMode="decimal"
            value={principal}
            onChange={(e) => setPrincipal(e.target.value)}
            disabled={busy}
          />
        </label>
        <label>
          Effective date
          <input
            aria-label="Effective date"
            type="date"
            value={effectiveOn}
            min={debt.opened_on}
            onChange={(e) => setEffectiveOn(e.target.value)}
            disabled={busy}
          />
        </label>
        {spec.opening ? (
          <label className="debt-event-check">
            <input
              type="checkbox"
              checked={openingBalance}
              onChange={(e) => setOpeningBalance(e.target.checked)}
              disabled={busy}
            />
            Opening balance — a pre-existing obligation with no observed funding
          </label>
        ) : null}
        {needsCategory ? (
          <label>
            Expense category
            <input
              aria-label="Expense category"
              value={expenseCategory}
              onChange={(e) => setExpenseCategory(e.target.value)}
              disabled={busy}
            />
          </label>
        ) : null}
      </div>

      {offLedger ? (
        <p className="muted-note">
          This event carries no source allocations — it records an off-ledger amount.
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
          known={seed ? [seed] : undefined}
        />
      )}

      <label className="debt-event-reason">
        Reason
        <textarea
          aria-label="Reason"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          disabled={busy}
          rows={2}
        />
      </label>

      {error ? <p className="debt-event-error">{error}</p> : null}
      <div className="debt-event-actions">
        <Button variant="ghost" size="sm" onClick={onCancel} disabled={busy}>Cancel</Button>
        <Button size="sm" onClick={submit} disabled={busy}>
          {busy ? 'Saving…' : 'Preview and save'}
        </Button>
      </div>
    </div>
  )
}
