import { useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { ApiError } from '@/lib/api'
import { EntryPicker } from './EntryPicker'
import { settlePlan, type EntryCandidate, type InstallmentDetail } from './api'

/**
 * Record paying a plan off early (INSTALLMENT_SETTLEMENT_SET).
 *
 * The three components are kept apart rather than collapsed into one "amount":
 * principal retires the obligation, a fee is a real cost, and a waiver is an
 * amount the issuer dropped. Netting them would report a settlement that cost
 * more or less than it did.
 */
export function SettlementForm({
  detail,
  generation,
  onSaved,
  onCancel,
}: {
  detail: InstallmentDetail
  generation: string
  onSaved: (status: string) => void | Promise<void>
  onCancel: () => void
}) {
  const plan = detail.plan
  const [settledOn, setSettledOn] = useState(plan.opened_on)
  const [source, setSource] = useState<EntryCandidate | null>(null)
  const [principal, setPrincipal] = useState('')
  const [fee, setFee] = useState('')
  const [waiver, setWaiver] = useState('')
  const [reason, setReason] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const inFlight = useRef(false)

  /**
   * Seed the date and principal from the transaction just chosen.
   *
   * The date is the transaction's own, and the principal defaults to what is
   * still unclaimed on it — the common case, where the whole payment retires
   * principal. Both stay editable: a settlement that carries a fee or a waiver
   * splits that same total differently, and only the operator knows how.
   */
  function pick(candidate: EntryCandidate) {
    setSource(candidate)
    setSettledOn(candidate.effective_at.slice(0, 10))
    if (!principal.trim()) setPrincipal(candidate.open_amount)
  }

  async function submit() {
    if (inFlight.current) return
    if (!source || !reason.trim()) {
      setError('Choose the settling transaction and give a reason.')
      return
    }
    inFlight.current = true
    setBusy(true)
    setError('')
    try {
      const result = await settlePlan({
        planId: plan.plan_id,
        generation,
        settledOn,
        sourceEntryAnchor: source.entry_anchor,
        principal: (principal.trim() || '0'),
        fee: (fee.trim() || '0'),
        waiver: (waiver.trim() || '0'),
        reason: reason.trim(),
      })
      await onSaved(result.status)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not record the settlement.')
    } finally {
      inFlight.current = false
      setBusy(false)
    }
  }

  return (
    <div className="debt-event-form">
      <h3>Settle early · {plan.merchant_label}</h3>
      <p className="muted-note">
        Splits the settling payment into principal, fee and waived amount. Each is recorded
        separately so the plan's real cost stays visible.
      </p>
      <EntryPicker
        planId={plan.plan_id}
        purpose="settlement"
        generation={generation}
        currency={plan.currency}
        selected={source?.entry_anchor ?? null}
        onPick={pick}
      />

      <div className="debt-event-grid">
        <label>
          Settled on
          <input
            aria-label="Settled on"
            type="date"
            value={settledOn}
            onChange={(e) => setSettledOn(e.target.value)}
            disabled={busy}
          />
        </label>
        <label>
          Principal ({plan.currency})
          <input
            aria-label="Settlement principal"
            inputMode="decimal"
            value={principal}
            onChange={(e) => setPrincipal(e.target.value)}
            disabled={busy}
          />
        </label>
        <label>
          Fee ({plan.currency})
          <input
            aria-label="Settlement fee"
            inputMode="decimal"
            value={fee}
            onChange={(e) => setFee(e.target.value)}
            disabled={busy}
          />
        </label>
        <label>
          Waived ({plan.currency})
          <input
            aria-label="Settlement waiver"
            inputMode="decimal"
            value={waiver}
            onChange={(e) => setWaiver(e.target.value)}
            disabled={busy}
          />
        </label>
      </div>

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
