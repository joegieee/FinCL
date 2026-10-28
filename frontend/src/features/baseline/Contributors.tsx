import { useEffect, useState } from 'react'
import { ArrowRight, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { shortCategory } from '@/lib/api'
import { reportedMoney } from '@/lib/workspaceReporting'
import { encodeAnchor } from '@/features/transactions/reportedApi'
import { loadBaselineContributors, type BaselineContributors as Contributors, type DecompositionPart } from './api'

export const PART_LABEL: Record<DecompositionPart, string> = {
  identified_recurring: 'Recurring',
  trip: 'Trips',
  unresolved_irregular: 'Irregular (held out for review)',
  routine_variable_residual: 'Routine residual',
  adjustments: 'Adjustments (refunds and reimbursements)',
}

/**
 * The components behind one month of the baseline decomposition, grouped by
 * the partition term the estimator assigned them to. Each group's total comes
 * from the server and equals the matching cell of the month table.
 */
export function BaselineContributors({ currency, month, onClose, onOpenTransaction }: {
  currency: string
  month: string
  onClose: () => void
  onOpenTransaction?: (encodedAnchor: string) => Promise<void>
}) {
  const [read, setRead] = useState<Contributors | null>(null)
  const [error, setError] = useState('')
  const [opening, setOpening] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    setRead(null); setError('')
    loadBaselineContributors(currency, month)
      .then(result => { if (live) setRead(result) })
      .catch(e => { if (live) setError((e as Error).message) })
    return () => { live = false }
  }, [currency, month])

  async function open(componentId: string, anchor: string) {
    if (!onOpenTransaction) return
    setOpening(componentId); setError('')
    try { await onOpenTransaction(encodeAnchor(anchor)) } catch (e) { setError((e as Error).message) } finally { setOpening(null) }
  }

  return (
    <section className="baseline-contributors" aria-label={`Contributors for ${month}`}>
      <div className="panel-title">
        <div><p className="eyebrow">CONTRIBUTORS · {currency}</p><h3>{month}</h3></div>
        <Button variant="ghost" size="sm" onClick={onClose}><X size={14} /> Close</Button>
      </div>
      {error && <div className="alert error" role="alert"><p>{error}</p></div>}
      {!read && !error && <p className="muted-note">Loading contributors…</p>}
      {read && !read.available && <p className="muted-note">{read.reason}</p>}
      {read?.available && <>
        {!read.reconciles && <p className="form-error">These contributors do not add up to the published month figures.</p>}
        {read.parts.map(part => (
          <div key={part.part} className="baseline-contributor-part">
            <h4>{PART_LABEL[part.part]} · {reportedMoney(part, 'total', currency)}{!part.reconciles && <small className="form-error"> does not reconcile</small>}</h4>
            {part.contributors.length === 0
              ? <p className="muted-note">None this month.</p>
              : <table className="pocket-table">
                  <thead><tr><th>Date</th><th>Merchant</th><th>Category</th><th>Amount</th>{onOpenTransaction && <th />}</tr></thead>
                  <tbody>{part.contributors.map(c => (
                    <tr key={c.component_id}>
                      <td>{c.date}{c.precision === 'MONTH' ? ' · month precision' : ''}</td>
                      <td>{c.merchant}{c.bucket === 'fees' ? ' · fee' : ''}</td>
                      <td>{shortCategory(c.category_key)}</td>
                      <td>{reportedMoney(c, 'amount', currency)}</td>
                      {onOpenTransaction && <td><button className="linkish" disabled={opening === c.component_id} onClick={() => void open(c.component_id, c.entry_anchor)}>{opening === c.component_id ? 'Opening…' : 'Open transaction'} <ArrowRight size={12} /></button></td>}
                    </tr>
                  ))}</tbody>
                </table>}
          </div>
        ))}
      </>}
    </section>
  )
}
