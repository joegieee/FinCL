import { useCallback, useEffect, useRef, useState } from 'react'
import { ChevronLeft, ChevronRight, Search } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Money } from '@/components/Money'
import { ApiError, money, shortCategory } from '@/lib/api'
import { dateLabel } from '@/lib/format'
import { loadReportingPolicy, type ReportingPolicySummary } from '@/features/analysis/reportedApi'
import { BulkEditor } from './BulkEditor'
import {
  type TransactionFilters, type TransactionRowOut, type TransactionsPageOut,
  emptyFilters, filtersActive, loadReportedTransactions, scopeKey,
} from './reportedApi'

export { StatementDetailPanel } from './StatementDetailPanel'

type StaleBanner = 'none' | 'same-scope' | 'refresh-required'
type LastGood = { key: string; policyCurrency: string; pages: Map<string, TransactionsPageOut> }

function cursorKey(cursor: string | null): string { return cursor ?? '__first__' }

/**
 * All-currency Transactions (M3b.2 step 08). One `GET /api/v1/reporting/
 * transactions` read drives the table over *every* pocket by default; the
 * only record filter this screen owns beyond the legacy set is the explicit,
 * locally-visible Account-currency (`pocket_currency`) filter — never the
 * ledger's reporting-currency setting, shown here only as a caption (§6.3).
 */
export function TransactionsView({
  start, end, generation, freshness, categories, accounts, currencies,
  initialPocketCurrency, initialCategory, initialSearch, initialKind,
  onOpenEntry, onSaved,
}: {
  start: string
  end: string
  generation: string
  freshness: string
  categories: string[]
  accounts: string[]
  currencies: string[]
  // §6.3: a legacy `?currency=USD` URL seeds this screen's own explicit,
  // clearable Account-currency filter once, on first mount only.
  initialPocketCurrency: string
  // Drill-through from Analysis/Overview (`App.tsx`'s `drill()`) re-applies
  // these on every change, even while this view stays mounted.
  initialCategory: string
  initialSearch: string
  initialKind: string
  onOpenEntry: (row: TransactionRowOut) => void
  onSaved: (status: string) => Promise<void>
}) {
  const [filters, setFilters] = useState<TransactionFilters>(() => ({
    ...emptyFilters, pocketCurrency: initialPocketCurrency, category: initialCategory, search: initialSearch, kind: initialKind,
  }))
  const [searchInput, setSearchInput] = useState(initialSearch)
  const [page, setPage] = useState<TransactionsPageOut | null>(null)
  const [cursors, setCursors] = useState<Array<string | null>>([null])
  const [index, setIndex] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [staleBanner, setStaleBanner] = useState<StaleBanner>('none')
  const [policy, setPolicy] = useState<ReportingPolicySummary | null>(null)
  const [picked, setPicked] = useState<Set<string>>(() => new Set())
  const [matchAll, setMatchAll] = useState<{ anchors: string[]; truncated: boolean } | null>(null)
  const lastPicked = useRef<number | null>(null)
  const lastGood = useRef<LastGood | null>(null)
  const seq = useRef(0)

  useEffect(() => { loadReportingPolicy().then(setPolicy).catch(() => {}) }, [])

  // A drill-through link (Analysis, Overview) re-applies its own filters even
  // when this view stays mounted; the account filter always resets with it,
  // matching the legacy `drill()`'s own reset.
  useEffect(() => {
    setFilters(f => (f.category === initialCategory && f.search === initialSearch && f.kind === initialKind && f.account === ''
      ? f
      : { ...f, category: initialCategory, search: initialSearch, kind: initialKind, account: '' }))
    setSearchInput(initialSearch)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialCategory, initialSearch, initialKind])

  useEffect(() => {
    const h = setTimeout(() => setFilters(f => (f.search === searchInput ? f : { ...f, search: searchInput })), 300)
    return () => clearTimeout(h)
  }, [searchInput])

  const key = scopeKey({ historyStart: start, historyEnd: end, filters })

  const fetchPage = useCallback((cursorList: Array<string | null>, pageIndex: number) => {
    const id = ++seq.current
    const cursor = cursorList[pageIndex]
    const ck = cursorKey(cursor)
    setLoading(true); setError('')
    loadReportedTransactions({ historyStart: start, historyEnd: end, filters, cursor, generation })
      .then(response => {
        if (id !== seq.current) return
        if (!lastGood.current || lastGood.current.key !== key) {
          lastGood.current = { key, policyCurrency: response.reporting_currency, pages: new Map() }
        }
        lastGood.current.policyCurrency = response.reporting_currency
        lastGood.current.pages.set(ck, response)
        setPage(response)
        setCursors(cs => { const next = cs.slice(0, pageIndex + 1); next[pageIndex + 1] = response.next_cursor; return next })
        setIndex(pageIndex)
        setStaleBanner('none')
      })
      .catch(async caught => {
        if (id !== seq.current) return
        if (caught instanceof ApiError && caught.status === 409) {
          const saved = lastGood.current
          // Same requested scope and page: show the saved page with its own
          // provenance, never a different scope's rows (§6.2). A changed or
          // stale cursor for a *new* scope falls straight through to
          // refresh-required below rather than reusing an unrelated page.
          if (saved && saved.key === key && saved.pages.has(ck)) {
            try {
              const current = await loadReportingPolicy()
              if (id !== seq.current) return
              setPolicy(current)
              if (current.currency === saved.policyCurrency) {
                setPage(saved.pages.get(ck) ?? null); setIndex(pageIndex); setStaleBanner('same-scope')
                return
              }
            } catch { /* fall through to refresh-required below */ }
          }
          lastGood.current = null
          setPage(null); setStaleBanner('refresh-required')
          return
        }
        setError((caught as Error).message); setPage(null); setStaleBanner('none')
      })
      .finally(() => { if (id === seq.current) setLoading(false) })
  }, [start, end, filters, generation, key])

  // Refetch page 1 whenever the window, generation or filters/sort change.
  useEffect(() => { setCursors([null]); fetchPage([null], 0) }, [start, end, generation, filters]) // eslint-disable-line react-hooks/exhaustive-deps
  // A frozen "select matching" list is not re-scoped by a later filter change.
  useEffect(() => { setPicked(new Set()); setMatchAll(null) }, [start, end, generation, filters])

  const rows = page?.rows ?? []
  function clearFilters() { setFilters(f => ({ ...emptyFilters, pocketCurrency: f.pocketCurrency, sort: f.sort })) }

  function togglePick(anchor: string, rowIndex?: number, range = false) {
    setMatchAll(null)
    setPicked(s => {
      const n = new Set(s)
      if (range && rowIndex !== undefined && lastPicked.current !== null) {
        const from = Math.min(rowIndex, lastPicked.current), to = Math.max(rowIndex, lastPicked.current)
        rows.slice(from, to + 1).forEach(row => n.add(row.entry.anchor))
      } else if (n.has(anchor)) n.delete(anchor)
      else n.add(anchor)
      return n
    })
    if (rowIndex !== undefined) lastPicked.current = rowIndex
  }
  function clearPicks() { setPicked(new Set()); setMatchAll(null) }
  function selectPage() { setMatchAll(null); setPicked(s => { const n = new Set(s); rows.forEach(r => n.add(r.entry.anchor)); return n }) }

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.metaKey || event.ctrlKey || event.altKey) return
      const target = event.target as HTMLElement
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)) return
      if (event.key === 'b' && picked.size) { event.preventDefault(); document.querySelector<HTMLElement>('.bulk-assign')?.focus() }
      if (event.key === 'e' && picked.size === 1) {
        const row = rows.find(item => picked.has(item.entry.anchor))
        if (row) { event.preventDefault(); onOpenEntry(row) }
      }
      if (event.key === 'Escape') clearPicks()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [picked, rows, onOpenEntry])

  return (
    <Card className="transaction-card">
      <div className="transaction-filters">
        <div className="search-field"><Search size={16} /><Input placeholder="Search merchant or description…" aria-label="Search transactions" value={searchInput} onChange={e => setSearchInput(e.target.value)} /></div>
        <select aria-label="Account currency filter" value={filters.pocketCurrency} onChange={e => setFilters(f => ({ ...f, pocketCurrency: e.target.value }))}>
          <option value="">All pockets</option>
          {currencies.map(c => <option key={c}>{c}</option>)}
        </select>
        <select aria-label="Filter category" value={filters.category} onChange={e => setFilters(f => ({ ...f, category: e.target.value }))}>
          <option value="">All categories</option>
          {categories.map(c => <option key={c}>{c}</option>)}
        </select>
        <select aria-label="Filter account" value={filters.account} onChange={e => setFilters(f => ({ ...f, account: e.target.value }))}>
          <option value="">All accounts</option>
          {accounts.map(a => <option key={a}>{a}</option>)}
        </select>
        <select aria-label="Transaction type" value={filters.kind} onChange={e => setFilters(f => ({ ...f, kind: e.target.value }))}>
          <option value="">All types</option>
          {['spending', 'income', 'refunds', 'fees', 'pending_review', 'internal', 'review'].map(k => <option key={k} value={k}>{k.replaceAll('_', ' ')}</option>)}
        </select>
        <select aria-label="Filter direction" value={filters.direction} onChange={e => setFilters(f => ({ ...f, direction: e.target.value }))}>
          <option value="">Any direction</option>
          <option value="out">Money out</option>
          <option value="in">Money in</option>
        </select>
        <select aria-label="Filter spending pattern" value={filters.behavior} onChange={e => setFilters(f => ({ ...f, behavior: e.target.value }))}>
          <option value="">Any spending pattern</option>
          {['fixed_recurring', 'variable_routine', 'irregular', 'unresolved'].map(v => <option key={v} value={v}>{v.replaceAll('_', ' ')}</option>)}
        </select>
        <select aria-label="Filter need" value={filters.need} onChange={e => setFilters(f => ({ ...f, need: e.target.value }))}>
          <option value="">Any need</option>
          {['essential', 'discretionary', 'unresolved'].map(v => <option key={v} value={v}>{v}</option>)}
        </select>
        <select aria-label="Filter basis" value={filters.basis} onChange={e => setFilters(f => ({ ...f, basis: e.target.value }))}>
          <option value="">Any basis</option>
          {['manual', 'recurring evidence', 'role rule', 'outlier review', 'category default'].map(v => <option key={v} value={v}>{v}</option>)}
        </select>
      </div>
      <div className="filter-secondary">
        <span>{page ? `${page.total} transaction${page.total === 1 ? '' : 's'}` : 'Loading…'}{policy && ` · Reporting in ${policy.currency}`}</span>
        <div>
          <label><input type="checkbox" checked={filters.unresolvedOnly} onChange={e => setFilters(f => ({ ...f, unresolvedOnly: e.target.checked }))} /> Unresolved only</label>
          <label><input type="checkbox" checked={filters.includeExcluded} onChange={e => setFilters(f => ({ ...f, includeExcluded: e.target.checked }))} /> Include excluded records</label>
          <select aria-label="Sort transactions" value={filters.sort} onChange={e => setFilters(f => ({ ...f, sort: e.target.value as TransactionFilters['sort'] }))}>
            <option value="date">Newest first</option>
            <option value="amount">Largest outgoings</option>
            <option value="merchant">Merchant A–Z</option>
          </select>
          {filtersActive(filters) && <Button variant="ghost" size="sm" onClick={clearFilters}>Clear filters</Button>}
        </div>
      </div>
      {staleBanner === 'refresh-required' && (
        <div className="alert" role="alert">
          <div><strong>Refresh required.</strong><p>The ledger has changed since the last refresh (new evidence, a rate import, or a reporting-policy change). Refresh before reading this list.</p></div>
          <Button variant="outline" onClick={() => fetchPage(cursors, index)}>Refresh</Button>
        </div>
      )}
      {staleBanner === 'same-scope' && page && (
        <div className="alert" role="status">
          <div><strong>Showing the last loaded values for this exact page and filters.</strong><p>New ledger inputs are available. Refresh to recompute.</p></div>
          <Button variant="outline" onClick={() => fetchPage(cursors, index)}>Refresh</Button>
        </div>
      )}
      <div className="table-scroll" aria-busy={loading}>
        <table className="transactions-table">
          <thead>
            <tr>
              <th className="pick-cell"><input type="checkbox" aria-label="Select all rows on this page" checked={rows.length > 0 && rows.every(r => picked.has(r.entry.anchor))} onChange={ev => { setMatchAll(null); setPicked(s => { const n = new Set(s); rows.forEach(r => ev.target.checked ? n.add(r.entry.anchor) : n.delete(r.entry.anchor)); return n }) }} /></th>
              <th>Date</th><th>Merchant / description</th><th>Account</th><th>Category</th><th>Spending pattern</th><th>Need</th><th className="amount">Amount</th><th>Status</th><th><span className="sr-only">Details</span></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => {
              const e = row.entry
              // Defensive fallback matching the legacy Transactions table: a
              // non-spending/non-role-eligible entry may carry no cost_behavior/need.
              const costBehavior = e.cost_behavior ?? (e.bucket === 'spending' ? 'unresolved' : 'N/A')
              const costBehaviorBasis = e.cost_behavior_basis ?? 'unresolved'
              const need = e.need ?? (e.bucket === 'spending' ? 'unresolved' : 'N/A')
              const needBasis = e.need_basis ?? 'unresolved'
              return (
                <tr key={e.id} tabIndex={0} data-anchor={e.anchor} data-index={i} onKeyDown={ev => { if (ev.key === ' ' && (ev.target as HTMLElement).tagName === 'TR') { ev.preventDefault(); togglePick(e.anchor, i, ev.shiftKey) } }} className={picked.has(e.anchor) ? 'row-picked' : ''}>
                  <td className="pick-cell" data-label="Select"><input type="checkbox" aria-label={`Select ${e.merchant}`} checked={picked.has(e.anchor)} onChange={() => {}} onClick={ev => togglePick(e.anchor, i, (ev as unknown as { shiftKey: boolean }).shiftKey)} /></td>
                  <td className="date-cell" data-label="Date">{e.precision === 'MONTH' ? e.date.slice(0, 7) : dateLabel(e.date)}{e.precision === 'MONTH' && <small>Month precision</small>}</td>
                  <td data-label="Merchant"><strong>{e.merchant}</strong><small title={e.description}>{e.description}</small></td>
                  <td data-label="Account">{e.account}</td>
                  <td data-label="Category"><button className="category-chip" onClick={() => onOpenEntry(row)}>{shortCategory(e.category)}</button></td>
                  <td data-label="Spending pattern"><button className="category-chip" title={`Basis: ${costBehaviorBasis}`} onClick={() => onOpenEntry(row)}>{costBehavior.replaceAll('_', ' ')}</button></td>
                  <td data-label="Need"><button className="category-chip" title={`Basis: ${needBasis}`} onClick={() => onOpenEntry(row)}>{need}</button></td>
                  <td className="amount" data-label="Amount">
                    <Money value={row.amount} />
                    {row.amount.unavailable && <small>Native: {money(e.amount, e.currency)}</small>}
                    {row.fee && Number(row.fee.native.value) !== 0 && <small>Fee <Money value={row.fee} /></small>}
                  </td>
                  <td data-label="Status"><Badge variant="outline" className={e.review ? 'review-badge' : ''}>{!e.eligible ? 'Excluded' : e.review ? 'Needs review' : e.bucket.replaceAll('_', ' ')}</Badge></td>
                  <td data-label=""><Button variant="ghost" size="sm" onClick={() => onOpenEntry(row)} aria-label={`Details for ${e.merchant}`}><ChevronRight size={16} /></Button></td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {(picked.size > 0 || matchAll) && (
        <BulkEditor
          generation={generation} freshness={freshness} categories={categories}
          rows={rows} picked={picked} matchAll={matchAll} total={page?.total ?? 0}
          filters={{
            start, end, pocket_currency: filters.pocketCurrency || undefined, search: filters.search.trim(),
            category: filters.category, account: filters.account, kind: filters.kind, direction: filters.direction,
            behavior: filters.behavior, need: filters.need, basis: filters.basis,
            unresolved_only: filters.unresolvedOnly, include_excluded: filters.includeExcluded,
          }}
          onSelectPage={selectPage} onSelectMatch={setMatchAll} onClear={clearPicks} onSaved={onSaved}
        />
      )}
      {error && (
        <div className="alert error" role="alert">
          <div><strong>Couldn’t load transactions.</strong><p>{error}</p></div>
          <Button variant="outline" onClick={() => fetchPage(cursors, index)}>Try again</Button>
        </div>
      )}
      {!loading && !error && rows.length === 0 && <div className="empty"><h3>No matching transactions</h3><p>Try clearing filters or selecting a different period.</p></div>}
      <div className="pagination">
        <span>{page && page.total > 0 ? `Showing ${rows.length} of ${page.total}${index > 0 || page.next_cursor ? ` · page ${index + 1}` : ''}` : '—'}</span>
        <div>
          <Button variant="outline" size="sm" disabled={index === 0 || loading} onClick={() => fetchPage(cursors, index - 1)}><ChevronLeft size={14} /> Previous</Button>
          <Button variant="outline" size="sm" disabled={!page?.next_cursor || loading} onClick={() => fetchPage(cursors, index + 1)}>Next <ChevronRight size={16} /></Button>
        </div>
      </div>
    </Card>
  )
}
