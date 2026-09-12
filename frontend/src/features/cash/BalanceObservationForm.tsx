import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { ApiError } from '@/lib/api'
import { recordBalanceObservation, retireBalanceObservation } from './api'
import type { CashPocketOut } from './reportedApi'

function today() {
  const d = new Date()
  const local = new Date(d.getTime() - d.getTimezoneOffset() * 60000)
  return local.toISOString().slice(0, 10)
}

/**
 * Record or retire a manual balance observation for one account/currency pocket.
 *
 * A manual observation is a dated fact asserted directly — what you saw in a
 * bank app or a passbook line — for use only when there is no statement to
 * import. It carries the same evidentiary weight as a printed statement
 * balance: it needs an as-of date and a reason, is appended to the ledger
 * permanently, and can be retired later if it turns out wrong.
 *
 * It is not a substitute for statement coverage: on its own it proves only
 * that one balance on that one day. A supported cutoff spanning it still
 * needs a certified statement to bridge any gap around it — see the
 * "coverage-backed balance cutoff" note on the Cash page.
 */
export function BalanceObservationForm({
  pocket, generation, onSaved, onCancel,
}: {
  pocket: CashPocketOut
  generation: string
  onSaved: (status: string) => void | Promise<void>
  onCancel: () => void
}) {
  const existing = pocket.manual_observation
  const [mode, setMode] = useState<'record' | 'retire'>('record')
  const [observedOn, setObservedOn] = useState(today())
  const [balance, setBalance] = useState('')
  const [reason, setReason] = useState('')
  const [retireReason, setRetireReason] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submitRecord() {
    if (!balance.trim() || !reason.trim()) {
      setError('A balance and a reason are required.')
      return
    }
    setBusy(true); setError('')
    try {
      const result = await recordBalanceObservation(generation, {
        account_id: pocket.account_id,
        currency_pocket: pocket.currency_pocket,
        observed_on: observedOn,
        balance: balance.trim(),
        reason: reason.trim(),
      })
      await onSaved(result.status)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not record that observation.')
    } finally {
      setBusy(false)
    }
  }

  async function submitRetire() {
    if (!existing?.observation) return
    if (!retireReason.trim()) {
      setError('A reason is required to retire it.')
      return
    }
    setBusy(true); setError('')
    try {
      const result = await retireBalanceObservation(generation, existing.observation, retireReason.trim())
      await onSaved(result.status)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not retire that observation.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onCancel() }}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            Manual balance observation · {pocket.display_name ?? pocket.account_id} · {pocket.currency_pocket}
          </DialogTitle>
          <DialogDescription>
            A dated fact asserted directly — what you saw in a bank app or a passbook — for when
            there is no statement to import. It carries the same weight as a printed statement
            balance: it needs an as-of date and a reason, is appended to your ledger permanently,
            and can be retired later if it is wrong. On its own it proves only that one balance on
            that one day; a supported cutoff spanning it still needs certified statement coverage
            to bridge any gap around it.
          </DialogDescription>
        </DialogHeader>

        {existing && (
          <div className="balance-observation-existing">
            <p className="muted-note">
              Current manual observation: <strong>{existing.balance}</strong> as of {existing.as_of}.
            </p>
            <div className="debt-event-actions" style={{ justifyContent: 'flex-start' }}>
              <Button variant={mode === 'record' ? 'default' : 'outline'} size="sm" onClick={() => { setMode('record'); setError('') }} disabled={busy}>
                Record a new one
              </Button>
              <Button variant={mode === 'retire' ? 'default' : 'outline'} size="sm" onClick={() => { setMode('retire'); setError('') }} disabled={busy}>
                Retire this one
              </Button>
            </div>
          </div>
        )}

        {mode === 'record' ? (
          <div className="debt-event-grid">
            <label>
              Observed on
              <input aria-label="Observed on" type="date" value={observedOn} max={today()} onChange={(e) => setObservedOn(e.target.value)} disabled={busy} />
            </label>
            <label>
              {`Balance (${pocket.currency_pocket})`}
              <input aria-label={`Balance (${pocket.currency_pocket})`} placeholder="0.00" value={balance} onChange={(e) => setBalance(e.target.value)} disabled={busy} />
            </label>
            <label className="debt-event-reason">
              Reason
              <textarea aria-label="Reason" rows={2} value={reason} onChange={(e) => setReason(e.target.value)} disabled={busy} placeholder="Why record this by hand?" />
            </label>
          </div>
        ) : (
          <div className="debt-event-grid">
            <label className="debt-event-reason">
              Reason to retire
              <textarea aria-label="Reason to retire" rows={2} value={retireReason} onChange={(e) => setRetireReason(e.target.value)} disabled={busy} placeholder="Why is the recorded observation wrong?" />
            </label>
          </div>
        )}

        {error ? <p className="debt-event-error">{error}</p> : null}
        <div className="debt-event-actions">
          <Button variant="ghost" size="sm" onClick={onCancel} disabled={busy}>Cancel</Button>
          <Button size="sm" onClick={mode === 'record' ? submitRecord : submitRetire} disabled={busy || (mode === 'retire' && !existing?.observation)}>
            {busy ? 'Saving…' : mode === 'record' ? 'Save observation' : 'Retire observation'}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
