import { useEffect, useState } from 'react'
import { X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { money, shortCategory, type Contributor } from '@/lib/api'
import { loadContributors } from './api'

/**
 * The exact components behind whatever total the reader clicked. Server-paged
 * off the opaque handle, so it always matches the current generation and scope;
 * a stale handle returns 409 and the caller reopens Analysis.
 */
export function ContributorTable({
  title, handle, currency, onClose,
}: {
  title: string
  handle: string
  currency: string
  onClose: () => void
}) {
  const [rows, setRows] = useState<Contributor[]>([])
  const [cursor, setCursor] = useState<string | null | undefined>(undefined)
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let live = true
    setRows([]); setCursor(undefined); setLoading(true); setError('')
    loadContributors(handle)
      .then(page => { if (live) { setRows(page.components); setCursor(page.next_cursor); setTotal(page.total) } })
      .catch(e => { if (live) setError((e as Error).message) })
      .finally(() => { if (live) setLoading(false) })
    return () => { live = false }
  }, [handle])

  function more() {
    if (!cursor) return
    setLoading(true)
    loadContributors(handle, cursor)
      .then(page => { setRows(r => [...r, ...page.components]); setCursor(page.next_cursor) })
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
            <tr><th>Date</th><th>Merchant</th><th>Category</th><th>Role</th><th className="amount">Amount</th></tr>
          </thead>
          <tbody>
            {rows.map(c => (
              <tr key={c.component_id}>
                <td className="date-cell">{c.date}{c.precision === 'MONTH' && <small>Month precision</small>}</td>
                <td>{c.merchant}</td>
                <td>{shortCategory(c.category)}</td>
                <td>{c.role}{!c.nets && <small>excluded from net</small>}</td>
                <td className="amount">{money(c.value, currency)}</td>
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
