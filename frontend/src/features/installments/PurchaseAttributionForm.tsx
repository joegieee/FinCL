import { useRef, useState } from 'react'
import { Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { ApiError, money } from '@/lib/api'
import { EntryPicker } from './EntryPicker'
import { allocatePurchase, type EntryCandidate, type InstallmentDetail } from './api'

type Line = {
  entry: EntryCandidate
  componentRole: 'principal' | 'fee'
  amount: string
}

/**
 * Attribute the original purchase to a plan (INSTALLMENT_ALLOCATION_SET).
 *
 * A plan splits a charge that already sits in the ledger; this says which one,
 * and how much of it is principal versus financing fee. Several lines are
 * allowed because a purchase can be split across components, but the backend
 * claims each `(transaction, component)` pair once, so the same pair is refused
 * twice here rather than silently double-counting the purchase.
 */
export function PurchaseAttributionForm({
  detail,
  generation,
  onSaved,
  onCancel,
  seed,
}: {
  detail: InstallmentDetail
  generation: string
  onSaved: (status: string) => void | Promise<void>
  onCancel: () => void
  /** One purchase handed over from the classification editor, started as a
   * whole-remainder principal line like any other picked source. */
  seed?: EntryCandidate
}) {
  const plan = detail.plan
  const [lines, setLines] = useState<Line[]>(
    seed ? [{ entry: seed, componentRole: 'principal', amount: seed.open_amount }] : [],
  )
  const [reason, setReason] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const inFlight = useRef(false)

  function add(entry: EntryCandidate) {
    setError('')
    if (lines.some((l) => l.entry.entry_anchor === entry.entry_anchor)) return
    // Defaults to the whole unclaimed amount as principal, which is the ordinary
    // case; a financing fee is split out by changing the role on a second line.
    setLines([...lines, { entry, componentRole: 'principal', amount: entry.open_amount }])
  }

  function update(index: number, patch: Partial<Line>) {
    setLines(lines.map((l, i) => (i === index ? { ...l, ...patch } : l)))
  }

  async function submit() {
    if (inFlight.current) return
    if (lines.length === 0 || !reason.trim()) {
      setError('Choose at least one source transaction and give a reason.')
      return
    }
    for (const line of lines) {
      const value = Number(line.amount)
      if (!Number.isFinite(value) || value <= 0) {
        setError('Every allocation needs a positive amount.')
        return
      }
      const ceiling = Number(
        line.componentRole === 'fee' ? line.entry.open_fee : line.entry.open_amount,
      )
      if (Number.isFinite(ceiling) && value > ceiling) {
        setError(
          `${line.entry.description ?? 'That transaction'} has only ` +
            `${line.componentRole === 'fee' ? line.entry.open_fee : line.entry.open_amount} ` +
            `unclaimed in its ${line.componentRole} component.`,
        )
        return
      }
    }
    const pairs = lines.map((l) => `${l.entry.entry_anchor}:${l.componentRole}`)
    if (new Set(pairs).size !== pairs.length) {
      setError('Each transaction component can only be claimed once.')
      return
    }
    inFlight.current = true
    setBusy(true)
    setError('')
    try {
      const result = await allocatePurchase({
        planId: plan.plan_id,
        generation,
        allocations: lines.map((l) => ({
          entry_anchor: l.entry.entry_anchor,
          component_role: l.componentRole,
          amount: l.amount.trim(),
        })),
        reason: reason.trim(),
      })
      await onSaved(result.status)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not attribute the purchase.')
    } finally {
      inFlight.current = false
      setBusy(false)
    }
  }

  return (
    <div className="debt-event-form">
      <h3>Attribute the original purchase · {plan.merchant_label}</h3>
      <p className="muted-note">
        Link the charge this plan splits. What you attribute here is the recognized cost of the
        purchase; the instalments themselves stay separate, so nothing is counted twice.
      </p>

      <EntryPicker
        planId={plan.plan_id}
        purpose="purchase"
        generation={generation}
        currency={plan.currency}
        selected={null}
        onPick={add}
      />

      {lines.length > 0 && (
        <table className="debt-history">
          <thead>
            <tr><th>Transaction</th><th>Component</th><th>Amount</th><th>Unclaimed</th><th/></tr>
          </thead>
          <tbody>
            {lines.map((line, index) => (
              <tr key={line.entry.entry_anchor}>
                <td>{line.entry.description ?? line.entry.entry_anchor.slice(0, 16) + '…'}</td>
                <td>
                  <select
                    aria-label={`Component for ${line.entry.description ?? line.entry.entry_anchor}`}
                    value={line.componentRole}
                    onChange={(e) =>
                      update(index, { componentRole: e.target.value as 'principal' | 'fee' })
                    }
                    disabled={busy}
                  >
                    <option value="principal">Principal</option>
                    <option value="fee">Fee</option>
                  </select>
                </td>
                <td>
                  <input
                    aria-label={`Amount for ${line.entry.description ?? line.entry.entry_anchor}`}
                    inputMode="decimal"
                    value={line.amount}
                    onChange={(e) => update(index, { amount: e.target.value })}
                    disabled={busy}
                  />
                </td>
                <td>
                  {money(
                    line.componentRole === 'fee' ? line.entry.open_fee : line.entry.open_amount,
                    plan.currency,
                  )}
                </td>
                <td>
                  <Button
                    variant="ghost"
                    size="sm"
                    aria-label={`Remove ${line.entry.description ?? line.entry.entry_anchor}`}
                    onClick={() => setLines(lines.filter((_, i) => i !== index))}
                    disabled={busy}
                  >
                    <Trash2 size={13} />
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
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
