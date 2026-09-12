import { useEffect, useState } from 'react'
import { ArrowRight, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Money } from '@/components/Money'
import { shortCategory } from '@/lib/api'
import { encodeAnchor } from '@/features/transactions/reportedApi'
import { loadReportedContributors, type ContributorRowOut } from './reportedApi'

/**
 * The exact reported components behind whatever total the reader clicked
 * (§6, step 04 item 6). Server-paged off the opaque reported handle, so it
 * always matches the current generation, reporting policy and rate
 * inventory; a mismatched handle returns 409 and the caller reopens Analysis.
 * Off-ledger components (a debt event with no bank row) show their own
 * `owning_source` — never a fabricated transaction id — and link to their
 * owning Debts tab instead (M3b.2 step 08, §5.3/§6.3 "safe anchor encoding").
 */
export function ContributorTable({
  title, handle, onClose, onOpenTransaction, onOpenDebts,
}: {
  title: string
  handle: string
  onClose: () => void
  // `owning_source.id` for a "transaction" row is the *natural* anchor of
  // the underlying ledger entry; it is encoded here (never server-side,
  // since `ContributorRowOut` carries no pre-encoded anchor) before being
  // handed to the same `GET /api/v1/reporting/transactions/{anchor}` route
  // the Transactions list itself opens details through.
  onOpenTransaction: (encodedAnchor: string) => Promise<void>
  onOpenDebts: () => void
}) {
  const [rows, setRows] = useState<ContributorRowOut[]>([])
  const [cursor, setCursor] = useState<string | null | undefined>(undefined)
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [opening, setOpening] = useState<string | null>(null)

  async function open(componentId: string, action: () => Promise<void>) {
    setOpening(componentId); setError('')
    try { await action() } catch (e) { setError((e as Error).message) } finally { setOpening(null) }
  }

  useEffect(() => {
    let live = true
    setRows([]); setCursor(undefined); setLoading(true); setError('')
    loadReportedContributors(handle)
      .then(page => { if (live) { setRows(page.rows); setCursor(page.next_cursor); setTotal(page.total) } })
      .catch(e => { if (live) setError((e as Error).message) })
      .finally(() => { if (live) setLoading(false) })
    return () => { live = false }
  }, [handle])

  function more() {
    if (!cursor) return
    setLoading(true)
    loadReportedContributors(handle, cursor)
      .then(page => { setRows(r => [...r, ...page.rows]); setCursor(page.next_cursor) })
      .catch(e => setError((e as Error).message))
      .finally(() => setLoading(false))
  }

  return (
    <Card className="contributor-table">
      <div className="panel-title">
        <div>
          <p className="eyebrow">EXACT CONTRIBUTORS</p>
          <h2>{title}</h2>
        </div>
        <Button variant="ghost" size="sm" onClick={onClose}><X size={14} /> Close</Button>
      </div>
      {error && <div className="alert error" role="alert"><p>{error}</p></div>}
      <div className="table-scroll">
        <table className="transactions-table">
          <thead>
            <tr><th>Date</th><th>Merchant</th><th>Category</th><th>Source</th><th>Role</th><th className="amount">Amount</th></tr>
          </thead>
          <tbody>
            {rows.map(c => (
              <tr key={c.component_id}>
                <td className="date-cell">{c.date}{c.precision === 'MONTH' && <small>Month precision</small>}</td>
                <td>{c.merchant ?? '—'}</td>
                <td>{shortCategory(c.category)}</td>
                <td>
                  {c.owning_source.kind === 'debt_event'
                    ? <button className="linkish" title={c.owning_source.id} onClick={onOpenDebts}>Off-ledger debt event <ArrowRight size={12} /></button>
                    : <button
                        className="linkish"
                        disabled={opening === c.component_id}
                        onClick={() => void open(c.component_id, () => onOpenTransaction(encodeAnchor(c.owning_source.id)))}
                      >
                        {opening === c.component_id ? 'Opening…' : 'Open transaction'} <ArrowRight size={12} />
                      </button>}
                </td>
                <td>{c.role}{!c.nets && <small>excluded from net</small>}</td>
                <td className="amount"><Money value={c.valuation} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!loading && !error && rows.length === 0 && <p className="empty-line">No components behind this total.</p>}
      <div className="pagination">
        <span>{total > 0 ? `Showing ${rows.length} of ${total} components` : '—'}</span>
        {cursor && <Button variant="outline" size="sm" disabled={loading} onClick={more}>Load more</Button>}
      </div>
      <div className="card-foot">
        Each row is one signed exact component. Displayed rows need not re-add to a rounded grand total.
      </div>
    </Card>
  )
}
