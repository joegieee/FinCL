import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { ApiError } from '@/lib/api'
import { certificationBases, certifyCoverage } from './api'

export type CertifyTarget = {
  account_id: string
  currency_pocket: string
  display_name: string | null
  period_start: string
  period_end: string
}

/**
 * Certify that one pocket had no movement across a date range.
 *
 * This is the second way to close a coverage gap, and the one that keeps the
 * existing evidence intact. A statement proves what happened in its period; a
 * certification proves that nothing happened in a range the statements do not
 * cover, so the last observed balance carries forward across it unchanged.
 *
 * It carries no amount at all — there is no balance field here and none in the
 * journalled decision — so it can never be used to assert money the ledger has
 * not seen. It can be retired later, and the days it covered become unproven
 * again the moment it is.
 */
export function CertifyCoverageForm({
  target, generation, onSaved, onCancel,
}: {
  target: CertifyTarget
  generation: string
  onSaved: (status: string) => void | Promise<void>
  onCancel: () => void
}) {
  const [start, setStart] = useState(target.period_start)
  const [end, setEnd] = useState(target.period_end)
  const [basis, setBasis] = useState<string>(certificationBases[0].value)
  const [reason, setReason] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit() {
    if (!reason.trim()) { setError('A reason is required.'); return }
    if (start > end) { setError('The start date must not be after the end date.'); return }
    setBusy(true); setError('')
    try {
      const result = await certifyCoverage(generation, {
        account_id: target.account_id,
        currency_pocket: target.currency_pocket,
        period_start: start,
        period_end: end,
        basis,
        reason: reason.trim(),
      })
      await onSaved(result.status)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not record that certification.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onCancel() }}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            Certify a quiet interval · {target.display_name ?? target.account_id} · {target.currency_pocket}
          </DialogTitle>
          <DialogDescription>
            Check this account’s own transaction list for the range below. If it shows no activity,
            certifying it lets the last observed balance carry forward across those days, so cash can
            be reported as of today instead of the last statement close. It records no amount — only
            that nothing happened — and can be retired later, which makes those days unproven again.
          </DialogDescription>
        </DialogHeader>

        <div className="debt-event-grid">
          <label>
            No movement from
            <input aria-label="No movement from" type="date" value={start} max={end} onChange={e => setStart(e.target.value)} disabled={busy} />
          </label>
          <label>
            Through
            <input aria-label="Through" type="date" value={end} max={target.period_end} onChange={e => setEnd(e.target.value)} disabled={busy} />
          </label>
          <label className="debt-event-reason">
            What did you check?
            <select aria-label="What did you check?" value={basis} onChange={e => setBasis(e.target.value)} disabled={busy}>
              {certificationBases.map(option => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </label>
          <label className="debt-event-reason">
            Reason
            <textarea
              aria-label="Reason" rows={2} value={reason} disabled={busy}
              onChange={e => setReason(e.target.value)}
              placeholder="What you checked, and where"
            />
          </label>
        </div>

        {error ? <p className="debt-event-error">{error}</p> : null}
        <div className="debt-event-actions">
          <Button variant="ghost" size="sm" onClick={onCancel} disabled={busy}>Cancel</Button>
          <Button size="sm" onClick={submit} disabled={busy}>
            {busy ? 'Saving…' : 'Certify this range'}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
