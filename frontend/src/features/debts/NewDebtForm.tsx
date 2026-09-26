import { useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { ApiError } from '@/lib/api'
import { previewThenCommit } from './api'

function today() {
  const d = new Date()
  const local = new Date(d.getTime() - d.getTimezoneOffset() * 60000)
  return local.toISOString().slice(0, 10)
}

/** Open a new PERSONAL_DEBT_OPEN — a payable ("I owe") or receivable ("owed to me"). */
export function NewDebtForm({
  generation,
  onSaved,
  onCancel,
  initial,
}: {
  generation: string
  /** `debtId` is the new debt's id, so a caller can go straight on to record
   * an event against it. */
  onSaved: (status: string, debtId: string) => void | Promise<void>
  onCancel: () => void
  /** Starting values, e.g. from a transaction handed over by the classification
   * editor: its currency, and a direction that transaction can back. */
  initial?: { direction?: 'PAYABLE' | 'RECEIVABLE'; currency?: string; openedOn?: string }
}) {
  const [direction, setDirection] = useState<'PAYABLE' | 'RECEIVABLE'>(initial?.direction ?? 'PAYABLE')
  const [counterpartyLabel, setCounterpartyLabel] = useState('')
  const [currency, setCurrency] = useState(initial?.currency ?? 'SGD')
  const [openedOn, setOpenedOn] = useState(initial?.openedOn ?? today())
  const [dueOn, setDueOn] = useState('')
  const [reason, setReason] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const inFlight = useRef(false)

  async function submit() {
    if (inFlight.current) return
    const label = counterpartyLabel.trim()
    const code = currency.trim().toUpperCase()
    if (!label || !reason.trim()) {
      setError('A counterparty label and a reason are required.')
      return
    }
    if (!/^[A-Z]{3}$/.test(code)) {
      setError('Currency must be a three-letter code, e.g. SGD.')
      return
    }
    if (dueOn && dueOn < openedOn) {
      setError('The due date may not be before the opened date.')
      return
    }
    inFlight.current = true; setBusy(true); setError('')
    const debtId = crypto.randomUUID()
    try {
      const result = await previewThenCommit({
        request_id: crypto.randomUUID(),
        expected_generation: generation,
        kind: 'PERSONAL_DEBT_OPEN',
        anchors: { debt: debtId },
        payload: {
          direction,
          currency: code,
          counterparty_label: label,
          opened_on: openedOn,
          due_on: dueOn || null,
          reason: reason.trim(),
        },
      })
      await onSaved(result.status, debtId)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not open the debt.')
    } finally {
      inFlight.current = false; setBusy(false)
    }
  }

  return (
    <div className="debt-event-form">
      <h3>New debt</h3>
      <div className="debt-event-grid">
        <label>
          Direction
          <select
            aria-label="Direction"
            value={direction}
            onChange={(e) => setDirection(e.target.value as 'PAYABLE' | 'RECEIVABLE')}
            disabled={busy}
          >
            <option value="PAYABLE">I owe them</option>
            <option value="RECEIVABLE">They owe me</option>
          </select>
        </label>
        <label>
          Counterparty
          <input
            aria-label="Counterparty label"
            placeholder="Who is it with?"
            value={counterpartyLabel}
            onChange={(e) => setCounterpartyLabel(e.target.value)}
            disabled={busy}
          />
        </label>
        <label>
          Currency
          <input
            aria-label="Currency"
            value={currency}
            onChange={(e) => setCurrency(e.target.value)}
            disabled={busy}
          />
        </label>
        <label>
          Opened on
          <input
            aria-label="Opened on"
            type="date"
            value={openedOn}
            onChange={(e) => setOpenedOn(e.target.value)}
            disabled={busy}
          />
        </label>
        <label>
          Due date (optional)
          <input
            aria-label="Due on"
            type="date"
            value={dueOn}
            min={openedOn}
            onChange={(e) => setDueOn(e.target.value)}
            disabled={busy}
          />
        </label>
      </div>

      <p className="muted-note">
        Saving opens the debt with these details, then takes you straight to its
        first event: record the amount as "Borrowed" / "Lent" (optionally as an
        opening balance).
      </p>

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
