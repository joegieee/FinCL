import { api, type components } from '@/lib/api'

// Wire shapes for the reported Cash workspace (M4 step 12, §3.3).
export type CashPageOut = components['schemas']['CashPageOut']
export type CashPocketOut = components['schemas']['CashPocketOut']
export type CashAtCutoffOut = components['schemas']['CashAtCutoffOut']
export type ValuedMoneyDTO = components['schemas']['ValuedMoneyDTO']

// Cash has no history range (native scope: 'none'). Its one explicit, locally
// visible record filter is the optional Account-currency `pocket_currency`
// (§6.3) -- reporting currency is never a filter here.
export type CashFilters = { pocketCurrency: string }

export const emptyCashFilters: CashFilters = { pocketCurrency: '' }

export function cashFiltersActive(f: CashFilters): boolean {
  return !!f.pocketCurrency
}

/** Mirrors `scopeKey` in `features/analysis/reportedApi.ts` (§6.2/§6.3): the
 * last-good cache key for one requested scope. */
export function cashScopeKey(opts: { asOf?: string; filters: CashFilters }): string {
  return JSON.stringify(opts)
}

export function loadReportedCash(
  opts: { asOf?: string; filters: CashFilters },
): Promise<CashPageOut> {
  const q = new URLSearchParams()
  if (opts.asOf) q.set('as_of', opts.asOf)
  if (opts.filters.pocketCurrency) q.set('pocket_currency', opts.filters.pocketCurrency)
  const qs = q.toString()
  return api<CashPageOut>('reporting/cash' + (qs ? '?' + qs : ''))
}

export {
  type CurrencyRegistryEntry, type ReportingPolicySummary, loadCurrencyRegistry, loadReportingPolicy,
} from '@/features/analysis/reportedApi'
