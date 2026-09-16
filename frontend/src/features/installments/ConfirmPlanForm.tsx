import { useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { ApiError, money } from '@/lib/api'
import { confirmPlan, type PlanCandidate } from './api'

/**
 * Confirm one proposed chain as an installment plan (INSTALLMENT_PLAN_SET).
 *
 * The occurrences are not editable here. They are the statement lines the issuer
 * printed, carried through verbatim; the operator confirms that they are one
 * plan and names it, which is the judgement the ledger cannot make on its own.
 */
export function ConfirmPlanForm({
  candidate,
  generation,
  onSaved,
  onCancel,
}: {
  candidate: PlanCandidate
  generation: string
  onSaved: (status: string) => void | Promise<void>
  onCancel: () => void
}) {
  const [merchantLabel, setMerchantLabel] = useState(candidate.merchant_label)
  const [planReference, setPlanReference] = useState('')
  // A complete chain has an exactly observed principal, so it is offered
  // prefilled. A partial one starts empty: the remaining instalments have not
  // happened, and `monthly x term` would be a guess wearing a fact's clothes.
  const [knownPrincipal, setKnownPrincipal] = useState(
    candidate.known_principal ? candidate.known_principal.value : '',
  )
  const [financingFee, setFinancingFee] = useState('')
  const [reason, setReason] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const inFlight = useRef(false)

  const missingTail = candidate.term_months - candidate.observed_count

  async function submit() {
    if (inFlight.current) return
    if (!merchantLabel.trim() || !reason.trim()) {
      setError('A name for the plan and a reason are both required.')
      return
    }
    inFlight.current = true
    setBusy(true)
    setError('')
    try {
      const result = await confirmPlan({
        candidate,
        generation,
        merchantLabel: merchantLabel.trim(),
        planReference: planReference.trim() || null,
        knownPrincipal: knownPrincipal.trim() || null,
        financingFee: financingFee.trim() || null,
        reason: reason.trim(),
      })
      await onSaved(result.status)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not confirm this plan.')
    } finally {
      inFlight.current = false
      setBusy(false)
    }
  }

  return (
    <div className="debt-event-form">
      <h3>Confirm a split payment · {candidate.family}</h3>
      <p className="muted-note">
        {candidate.observed_count} instalment{candidate.observed_count === 1 ? '' : 's'} of{' '}
        {candidate.term_months} printed on your statements, ordinals {candidate.first_ordinal}–
        {candidate.last_ordinal}, {money(candidate.observed_total.value, candidate.currency)} paid so far.
      </p>

      <table className="debt-history">
        <thead>
          <tr><th>n / of</th><th>Date</th><th>Amount</th><th>Statement line</th></tr>
        </thead>
        <tbody>
          {candidate.occurrences.map((o) => (
            <tr key={o.entry_anchor}>
              <td>{o.occurrence_index} / {o.occurrence_count}</td>
              <td>{o.effective_on}</td>
              <td>{money(o.amount, candidate.currency)}</td>
              <td>{o.description ?? '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="debt-event-grid">
        <label>
          Name this plan
          <input
            aria-label="Plan name"
            value={merchantLabel}
            onChange={(e) => setMerchantLabel(e.target.value)}
            disabled={busy}
          />
        </label>
        <label>
          Issuer plan reference (optional)
          <input
            aria-label="Plan reference"
            value={planReference}
            onChange={(e) => setPlanReference(e.target.value)}
            disabled={busy}
          />
        </label>
        <label>
          Purchase principal ({candidate.currency})
          <input
            aria-label="Known principal"
            inputMode="decimal"
            placeholder={candidate.complete ? '' : 'Leave blank if unknown'}
            value={knownPrincipal}
            onChange={(e) => setKnownPrincipal(e.target.value)}
            disabled={busy}
          />
        </label>
        <label>
          Financing fee (optional)
          <input
            aria-label="Financing fee"
            inputMode="decimal"
            value={financingFee}
            onChange={(e) => setFinancingFee(e.target.value)}
            disabled={busy}
          />
        </label>
      </div>

      {candidate.complete ? (
        <p className="muted-note">
          Every instalment of this plan has appeared, so the principal above is the exact
          observed total, not an estimate.
        </p>
      ) : (
        <p className="muted-note">
          {missingTail} instalment{missingTail === 1 ? '' : 's'} of {candidate.term_months} have not
          appeared on a statement yet. Leave the principal blank and the plan will show what has
          actually been paid with the rest marked unknown — enter it only from the purchase receipt.
        </p>
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
          {busy ? 'Saving…' : 'Preview and confirm'}
        </Button>
      </div>
    </div>
  )
}
