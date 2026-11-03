import { api, type components } from '@/lib/api'

// Wire shapes for the reported Analysis surface (M3b.1 steps 04-05).
export type ReportedAnalysis = components['schemas']['ReportedAnalysisOut']
export type ConsolidatedValuationDTO = components['schemas']['ConsolidatedValuationDTO']
export type SummaryEntryOut = components['schemas']['SummaryEntryOut']
export type RankedGroupOut = components['schemas']['RankedGroupOut']
export type TrendMonthOut = components['schemas']['TrendMonthOut']
export type ContributorsPageOut = components['schemas']['ContributorsPageOut']
export type ContributorRowOut = components['schemas']['ContributorRowOut']

export type AnalysisFilters = {
  comparison: 'auto' | 'previous_month' | 'preceding_months' | 'preceding_equal_duration' | 'none'
  trendRange: '6' | '12' | 'all'
  // §6.3: an explicit, locally-owned Account-currency record filter — never
  // the ledger's reporting currency, which this screen shows as a setting
  // (`reporting_currency` on the response) rather than a filter here.
  pocketCurrency: string
  account: string
  category: string
  merchant: string
}

export const emptyFilters: AnalysisFilters = {
  comparison: 'auto', trendRange: '6', pocketCurrency: '', account: '', category: '', merchant: '',
}

export function filtersActive(f: AnalysisFilters): boolean {
  return f.comparison !== 'auto' || f.trendRange !== '6' || !!f.pocketCurrency || !!f.account || !!f.category || !!f.merchant
}

/** One requested scope, used both to build the query string and as the
 * last-good cache key (§6.2/§6.3): two calls with the same key answer the
 * same question, so a stale response from one may stand in for the other,
 * but never for a different key. */
export function scopeKey(opts: { historyStart: string; historyEnd: string; asOf?: string; filters: AnalysisFilters }): string {
  return JSON.stringify(opts)
}

export function loadReportedAnalysis(
  opts: { historyStart: string; historyEnd: string; asOf?: string; filters: AnalysisFilters },
): Promise<ReportedAnalysis> {
  const q = new URLSearchParams({ history_start: opts.historyStart, history_end: opts.historyEnd })
  if (opts.asOf) q.set('as_of', opts.asOf)
  if (opts.filters.comparison !== 'auto') q.set('comparison', opts.filters.comparison)
  if (opts.filters.trendRange !== '6') q.set('trend_range', opts.filters.trendRange)
  if (opts.filters.pocketCurrency) q.set('pocket_currency', opts.filters.pocketCurrency)
  if (opts.filters.account) q.append('accounts', opts.filters.account)
  if (opts.filters.category) q.append('category_keys', opts.filters.category)
  if (opts.filters.merchant) q.set('merchant', opts.filters.merchant)
  return api<ReportedAnalysis>('reporting/analysis?' + q)
}

export function loadReportedContributors(handle: string, cursor?: string | null): Promise<ContributorsPageOut> {
  const q = new URLSearchParams({ limit: '100' })
  if (cursor) q.set('cursor', cursor)
  return api<ContributorsPageOut>(`reporting/contributors/${encodeURIComponent(handle)}?` + q)
}

// `/api/v1/reporting-currency` has no pydantic response_model yet (plan M1
// §7.1), so `lib/schema.d.ts` types its body as `unknown`; this is the
// smallest local shape this screen actually reads from it.
export type ReportingPolicySummary = {
  currency: string
  is_default: boolean
  policy_version: string | null
}

export async function loadReportingPolicy(): Promise<ReportingPolicySummary> {
  const data = await api<{ reporting_policy: ReportingPolicySummary }>('reporting-currency')
  return data.reporting_policy
}

// Same untyped body (`currency_registry_dto()`); the settings control (step
// 10, §6.3) needs the supported-currency list to populate its selector.
export type CurrencyRegistryEntry = { code: string; display_name: string; minor_unit_scale: number }

export async function loadCurrencyRegistry(): Promise<CurrencyRegistryEntry[]> {
  const data = await api<{ currency_registry: CurrencyRegistryEntry[] }>('reporting-currency')
  return data.currency_registry
}
