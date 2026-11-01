import { reportedMoney } from '@/lib/workspaceReporting'
import { useState } from 'react'
import { PiggyBank, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { ApiError, money } from '@/lib/api'
import { recordAllocation, retireAllocation, type Fund } from './api'
import { dateLabel } from '@/lib/format'

function today() {
  const d = new Date()
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10)
}

/**
 * Record and withdraw the contributions actually set aside for one fund.
 *
 * These were the last backend-only journal kinds in the funds workflow: the
 * product could show a shortfall and never let anyone close it. Recording a
 * contribution raises the fund's earmark and lowers unallocated cash by the
 * same amount, so total cash does not move and no day's balance changes. That
 * is deliberate and is stated on the form — a plan to save cannot rescue a cash
 * path, and a control that appeared to do so would be the more dangerous bug.
 */
export function AllocationControls({
  fund, generation, onSaved,
}: {
  fund: Fund
  generation: string
  onSaved: (status: string) => void | Promise<void>
}) {
  const [amount, setAmount] = useState('')
  const [on, setOn] = useState(today())
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')

  async function record() {
    if (!amount.trim()) { setError('An amount is required.'); return }
    setBusy('record'); setError('')
    try {
      const result = await recordAllocation(generation, fund.fund, {
        allocated_on: on,
        amount: amount.trim(),
        reason: `set aside toward ${fund.name}`,
      })
      setAmount('')
      await onSaved(result.status)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not record that contribution.')
    } finally {
      setBusy('')
    }
  }

  async function retire(allocation: string) {
    setBusy(allocation); setError('')
    try {
      const result = await retireAllocation(
        generation, fund.fund, allocation, 'this contribution was not actually set aside',
      )
      await onSaved(result.status)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not withdraw that contribution.')
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="fund-allocations">
      <div className="fund-allocation-row">
        <label>
          Amount set aside ({fund.currency})
          <Input
            aria-label={`Amount set aside for ${fund.name}`} placeholder="0.00"
            value={amount} disabled={busy !== ''} onChange={e => setAmount(e.target.value)}
          />
        </label>
        <label>
          On
          <Input
            aria-label={`Date set aside for ${fund.name}`} type="date" value={on}
            max={today()} disabled={busy !== ''} onChange={e => setOn(e.target.value)}
          />
        </label>
        <Button size="sm" disabled={busy !== ''} onClick={() => void record()}>
          <PiggyBank size={14} /> {busy === 'record' ? 'Recording…' : 'Record contribution'}
        </Button>
      </div>

      {error && <p className="debt-event-error" role="alert">{error}</p>}

      {fund.recorded_allocation.entries.length > 0 && (
        <ul className="issue-list">
          {fund.recorded_allocation.entries.map(entry => (
            <li key={entry.allocation}>
              <div className="remedy-body">
                <p><strong>{reportedMoney(entry, 'amount', fund.currency)}</strong> · {dateLabel(entry.allocated_on)}</p>
              </div>
              <Button
                variant="ghost" size="sm" disabled={busy !== ''}
                aria-label={`Withdraw the ${entry.amount} contribution on ${dateLabel(entry.allocated_on)}`}
                onClick={() => void retire(entry.allocation)}
              >
                <X size={14} /> Withdraw
              </Button>
            </li>
          ))}
        </ul>
      )}

      <p className="muted-note">
        A contribution earmarks cash you already hold: it raises this fund and lowers your
        unallocated cash by the same amount, so your total cash and every forecast balance stay
        exactly where they were. The money leaves only when the expense itself falls due.
      </p>
    </div>
  )
}
