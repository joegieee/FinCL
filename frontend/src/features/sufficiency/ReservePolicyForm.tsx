import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { ApiError } from '@/lib/api'
import { setReservePolicy } from './api'

/**
 * Choose the reserve policy: how many months of essential spending stays back.
 *
 * This form takes a month count and a reason, and deliberately no amount. The
 * number of months is a judgement only the operator can make — three suits a
 * salaried job, six suits contract work — while the monthly figure it multiplies
 * is the evidence-derived mean essential spend and stays calculated. Keeping the
 * two apart is what lets the policy be adjustable without turning it into a way
 * to type in a living cost.
 */
export function ReservePolicyForm({
  months, generation, onSaved, onCancel,
}: {
  months: string
  generation: string
  onSaved: (status: string) => void | Promise<void>
  onCancel: () => void
}) {
  const [value, setValue] = useState(months)
  const [reason, setReason] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit() {
    if (!reason.trim()) { setError('A reason is required.'); return }
    const parsed = Number(value)
    if (!Number.isFinite(parsed) || parsed <= 0 || parsed > 12) {
      setError('Choose between 0.5 and 12 months.')
      return
    }
    setBusy(true); setError('')
    try {
      const result = await setReservePolicy(generation, value.trim(), reason.trim())
      await onSaved(result.status)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not record that policy.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open onOpenChange={open => { if (!open) onCancel() }}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Choose your reserve policy</DialogTitle>
          <DialogDescription>
            How many months of essential spending should stay untouched? Three is the
            usual answer for steady salaried income; six is common for contract or
            variable income. Only the number of months is yours to choose — the monthly
            amount it multiplies is always derived from your own recorded essential
            spending, and is never entered here.
          </DialogDescription>
        </DialogHeader>

        <div className="debt-event-grid">
          <label>
            Months of essential spending
            <input
              aria-label="Months of essential spending" type="number"
              min="0.5" max="12" step="0.5" value={value} disabled={busy}
              onChange={e => setValue(e.target.value)}
            />
          </label>
          <label className="debt-event-reason">
            Why this many?
            <textarea
              aria-label="Why this many?" rows={2} value={reason} disabled={busy}
              onChange={e => setReason(e.target.value)}
              placeholder="e.g. my income is contract work with gaps between projects"
            />
          </label>
        </div>

        {error ? <p className="debt-event-error">{error}</p> : null}
        <div className="debt-event-actions">
          <Button variant="ghost" size="sm" onClick={onCancel} disabled={busy}>Cancel</Button>
          <Button size="sm" onClick={submit} disabled={busy}>
            {busy ? 'Saving…' : 'Save reserve policy'}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
