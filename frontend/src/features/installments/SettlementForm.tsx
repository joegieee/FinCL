import { useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { ApiError } from '@/lib/api'
import { settlePlan, type InstallmentDetail } from './api'

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
  const [sourceEntryAnchor, setSourceEntryAnchor] = useState('')
  const [principal, setPrincipal] = useState('')
  const [fee, setFee] = useState('')
  const [waiver, setWaiver] = useState('')
  const [reason, setReason] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const inFlight = useRef(false)

  async function submit() {
    if (inFlight.current) return
    if (!sourceEntryAnchor.trim() || !reason.trim()) {
      setError('The settling transaction and a reason are both required.')
      return
    }
    const components = [principal, fee, waiver].map((v) => Number(v || '0'))
    if (components.some((v) => !Number.isFinite(v) || v < 0)) {
      setError('Principal, fee and waiver must each be zero or a positive amount.')
      return
    }
    if (components.every((v) => v === 0)) {
      setError('A settlement needs a positive principal, fee or waiver.')
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
        sourceEntryAnchor: sourceEntryAnchor.trim(),
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
          Settling transaction anchor
          <input
            aria-label="Source entry anchor"
            value={sourceEntryAnchor}
            onChange={(e) => setSourceEntryAnchor(e.target.value)}
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
