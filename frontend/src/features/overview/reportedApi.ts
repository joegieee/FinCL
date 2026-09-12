import { api, type components } from '@/lib/api'

// Wire shapes for the composite Overview read (M3b.3 step 09-10, §3.3-3.4).
export type MoneyFlowOut = components['schemas']['MoneyFlowOut']
export type ConsolidatedValuationDTO = components['schemas']['ConsolidatedValuationDTO']
export type EconomicFlowOut = components['schemas']['EconomicFlowOut']
export type GrossBurnCardOut = components['schemas']['GrossBurnCardOut']
export type CashAtCutoffOut = components['schemas']['CashAtCutoffOut']
export type ForecastClosingOut = components['schemas']['ForecastClosingOut']

// Overview owns exactly one explicit, locally-visible record filter beyond
// the shared period (§6.3): the optional Account-currency `pocket_currency`.
// Reporting currency is never a filter here — it is shown as a setting via
// `ReportingPolicySettings` and resolved server-side from the ledger policy.
export type OverviewFilters = { pocketCurrency: string }

export const emptyOverviewFilters: OverviewFilters = { pocketCurrency: '' }

export function overviewFiltersActive(f: OverviewFilters): boolean {
  return !!f.pocketCurrency
}

/** Mirrors `scopeKey` in `features/analysis/reportedApi.ts` (§6.2/§6.3): the
 * last-good cache key for one requested scope. */
export function overviewScopeKey(opts: { historyStart: string; historyEnd: string; asOf?: string; filters: OverviewFilters }): string {
  return JSON.stringify(opts)
}

export function loadMoneyFlow(
  opts: { historyStart: string; historyEnd: string; asOf?: string; filters: OverviewFilters },
): Promise<MoneyFlowOut> {
  const q = new URLSearchParams({ history_start: opts.historyStart, history_end: opts.historyEnd })
  if (opts.asOf) q.set('as_of', opts.asOf)
  if (opts.filters.pocketCurrency) q.set('pocket_currency', opts.filters.pocketCurrency)
  return api<MoneyFlowOut>('money-flow?' + q)
}

export {
  type CurrencyRegistryEntry, type ReportingPolicySummary, loadCurrencyRegistry, loadReportingPolicy,
} from '@/features/analysis/reportedApi'
