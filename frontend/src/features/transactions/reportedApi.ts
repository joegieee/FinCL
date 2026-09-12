import { api, type components } from '@/lib/api'

// Wire shapes for the reported Transactions surface (M3b.2 step 07-08).
export type TransactionsPageOut = components['schemas']['TransactionsPageOut']
export type TransactionRowOut = components['schemas']['TransactionRowOut']
export type TransactionAnchorsOut = components['schemas']['TransactionAnchorsOut']
export type StatementMoneyDetailOut = components['schemas']['StatementMoneyDetailOut']
export type ExactAmountOut = components['schemas']['ExactAmountOut']
export type PrintedConversionOut = components['schemas']['PrintedConversionOut']
export type EffectiveRateOut = components['schemas']['EffectiveRateOut']
export type ValidationOut = components['schemas']['ValidationOut']
export type ProvenanceOut = components['schemas']['ProvenanceOut']

export type TransactionSort = 'date' | 'merchant' | 'amount'

export type TransactionFilters = {
  // §6.3: an explicit, locally-owned Account-currency record filter —
  // never the ledger's reporting currency, which is shown as a setting.
  pocketCurrency: string
  search: string
  category: string
  account: string
  kind: string
  direction: string
  behavior: string
  need: string
  basis: string
  unresolvedOnly: boolean
  includeExcluded: boolean
  sort: TransactionSort
}

export const emptyFilters: TransactionFilters = {
  pocketCurrency: '', search: '', category: '', account: '', kind: '', direction: '',
  behavior: '', need: '', basis: '', unresolvedOnly: false, includeExcluded: false, sort: 'date',
}

/** Matches the legacy "Clear filters" button's own scope (search..unresolvedOnly);
 * pocket currency and sort have their own controls and are not reset by it. */
export function filtersActive(f: TransactionFilters): boolean {
  return !!(f.search || f.category || f.account || f.kind || f.direction || f.behavior || f.need || f.basis || f.unresolvedOnly)
}

/** One requested scope, used both to build the query string and as the
 * last-good cache key (§6.2/§6.3): two calls with the same key answer the
 * same question, so a stale response from one may stand in for the other,
 * but never for a different key. */
export function scopeKey(opts: { historyStart: string; historyEnd: string; filters: TransactionFilters }): string {
  return JSON.stringify(opts)
}

function applyFilterParams(q: URLSearchParams, filters: TransactionFilters): void {
  if (filters.pocketCurrency) q.set('pocket_currency', filters.pocketCurrency)
  if (filters.search.trim()) q.set('search', filters.search.trim())
  if (filters.category) q.set('category', filters.category)
  if (filters.account) q.set('account', filters.account)
  if (filters.kind) q.set('kind', filters.kind)
  if (filters.direction) q.set('direction', filters.direction)
  if (filters.behavior) q.set('behavior', filters.behavior)
  if (filters.need) q.set('need', filters.need)
  if (filters.basis) q.set('basis', filters.basis)
  if (filters.unresolvedOnly) q.set('unresolved_only', 'true')
  if (filters.includeExcluded) q.set('include_excluded', 'true')
}

export function loadReportedTransactions(opts: {
  historyStart: string
  historyEnd: string
  asOf?: string
  filters: TransactionFilters
  cursor?: string | null
  limit?: number
  generation?: string
}): Promise<TransactionsPageOut> {
  const q = new URLSearchParams({
    history_start: opts.historyStart, history_end: opts.historyEnd,
    sort: opts.filters.sort, limit: String(opts.limit ?? 50),
  })
  if (opts.asOf) q.set('as_of', opts.asOf)
  if (opts.cursor) q.set('cursor', opts.cursor)
  if (opts.generation) q.set('generation', opts.generation)
  applyFilterParams(q, opts.filters)
  return api<TransactionsPageOut>('reporting/transactions?' + q)
}

export function loadReportedTransactionAnchors(opts: {
  historyStart: string
  historyEnd: string
  asOf?: string
  filters: TransactionFilters
  generation?: string
}): Promise<TransactionAnchorsOut> {
  const q = new URLSearchParams({ history_start: opts.historyStart, history_end: opts.historyEnd })
  if (opts.asOf) q.set('as_of', opts.asOf)
  if (opts.generation) q.set('generation', opts.generation)
  applyFilterParams(q, opts.filters)
  return api<TransactionAnchorsOut>('reporting/transaction-anchors?' + q)
}

export function loadReportedTransactionDetail(encodedAnchor: string, asOf?: string): Promise<TransactionRowOut> {
  const q = new URLSearchParams()
  if (asOf) q.set('as_of', asOf)
  const query = q.toString()
  return api<TransactionRowOut>(`reporting/transactions/${encodedAnchor}${query ? '?' + query : ''}`)
}

/** Client-side mirror of `reporting_transactions.encode_anchor` — a URL-safe
 * transport encoding of a natural anchor string, byte-identical to Python's
 * `base64.urlsafe_b64encode` (standard alphabet with `+`/`/` swapped for
 * `-`/`_`, padding preserved). Only used to open a transaction's reported
 * detail from a contributor row that carries the *natural* anchor
 * (`owning_source.id`) but not a pre-encoded one. */
export function encodeAnchor(natural: string): string {
  const bytes = new TextEncoder().encode(natural)
  let binary = ''
  bytes.forEach(byte => { binary += String.fromCharCode(byte) })
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_')
}
