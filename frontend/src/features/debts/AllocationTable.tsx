import { useEffect, useState } from 'react'
import { Plus, X } from 'lucide-react'
import { ApiError, money } from '@/lib/api'
import {
  loadAllocationCandidates,
  type AllocationCandidate,
  type AllocationInput,
} from './api'

const ZERO = '0.00'

export function emptyAllocation(anchor = ''): AllocationInput {
  return {
    entry_anchor: anchor,
    principal_amount: ZERO,
    interest_amount: ZERO,
    fee_amount: ZERO,
    separate_fee_amount: ZERO,
  }
}

function toMinor(value: string): number {
  const n = Number(value)
  return Number.isFinite(n) ? Math.round(n * 100) : NaN
}

/** Sum of the allocation principals, or NaN if any field is unparseable. */
export function principalTotalMinor(rows: AllocationInput[]): number {
  return rows.reduce((acc, r) => acc + toMinor(r.principal_amount), 0)
}

export function AllocationTable({
  debtId,
  eventKind,
  generation,
  currency,
  rows,
  onChange,
  eventPrincipal,
}: {
  debtId: string
  eventKind: string
  generation: string
  currency: string
  rows: AllocationInput[]
  onChange: (rows: AllocationInput[]) => void
  eventPrincipal: string
}) {
  const [candidates, setCandidates] = useState<AllocationCandidate[]>([])
  const [note, setNote] = useState('')

  useEffect(() => {
    let live = true
    setNote('')
    loadAllocationCandidates({ debtId, eventKind, generation })
      .then((p) => { if (live) setCandidates(p.candidates) })
      .catch((e) => {
        if (live) setNote(e instanceof ApiError ? e.message : 'Could not load source transactions.')
      })
    return () => { live = false }
  }, [debtId, eventKind, generation])

  function patch(index: number, field: keyof AllocationInput, value: string) {
    onChange(rows.map((r, i) => (i === index ? { ...r, [field]: value } : r)))
  }

  const totalMinor = principalTotalMinor(rows)
  const wantMinor = toMinor(eventPrincipal)
  const balanced = Number.isFinite(totalMinor) && totalMinor === wantMinor

  return (
    <div className="debt-alloc">
      <div className="debt-alloc-head">
        <span>Source transactions</span>
        <small>
          borrowed / returned principal is a bank inflow; lent / repaid principal is an outflow
        </small>
      </div>
      {note ? <p className="debt-empty">{note}</p> : null}
      <table>
        <thead>
          <tr>
            <th>Transaction</th><th>Principal</th><th>Interest</th>
            <th>Fee</th><th>Separate fee</th><th />
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => {
            const picked = candidates.find((c) => c.entry_anchor === row.entry_anchor)
            return (
              <tr key={index}>
                <td>
                  <select
                    aria-label={`Allocation ${index + 1} source`}
                    value={row.entry_anchor}
                    onChange={(e) => patch(index, 'entry_anchor', e.target.value)}
                  >
                    <option value="">Select a transaction…</option>
                    {row.entry_anchor && !picked ? (
                      <option value={row.entry_anchor}>{row.entry_anchor} (unresolved)</option>
                    ) : null}
                    {candidates.map((c) => (
                      <option key={c.entry_anchor} value={c.entry_anchor}>
                        {c.effective_at.slice(0, 10)} · {money(c.amount, currency)} · {money(c.open_amount, currency)} open
                      </option>
                    ))}
                  </select>
                </td>
                {(['principal_amount', 'interest_amount', 'fee_amount', 'separate_fee_amount'] as const).map(
                  (field) => (
                    <td key={field}>
                      <input
                        aria-label={`Allocation ${index + 1} ${field.replace('_amount', '')}`}
                        inputMode="decimal"
                        value={row[field]}
                        onChange={(e) => patch(index, field, e.target.value)}
                      />
                    </td>
                  ),
                )}
                <td>
                  <button
                    type="button"
                    aria-label={`Remove allocation ${index + 1}`}
                    onClick={() => onChange(rows.filter((_, i) => i !== index))}
                  >
                    <X size={13} />
                  </button>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
      <div className="debt-alloc-foot">
        <button type="button" onClick={() => onChange([...rows, emptyAllocation()])}>
          <Plus size={13} /> Add a source
        </button>
        <span className={balanced ? 'ok' : 'warn'}>
          Allocated principal {money((totalMinor / 100).toFixed(2), currency)} of{' '}
          {money(eventPrincipal, currency)}
          {balanced ? '' : ' — must match the event principal'}
        </span>
      </div>
    </div>
  )
}
