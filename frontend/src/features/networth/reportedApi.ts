import { api, type components } from '@/lib/api'

// Wire shapes for the reported Net worth workspace (M4 step 13, §3.3).
export type NetWorthPageOut = components['schemas']['NetWorthPageOut']
export type NetWorthComponentOut = components['schemas']['NetWorthComponentOut']
export type NetWorthTotalsOut = components['schemas']['NetWorthTotalsOut']
export type NetWorthTrendOut = components['schemas']['NetWorthTrendOut']
export type NativeCurrencySummaryOut = components['schemas']['NativeCurrencySummaryOut']
export type ConsolidatedValuationDTO = components['schemas']['ConsolidatedValuationDTO']

/** Mirrors `scopeKey` in `features/analysis/reportedApi.ts` (§6.2): the
 * last-good cache key for one requested scope. Net worth has no record
 * filter of its own -- only the two dated observations. */
export function netWorthScopeKey(opts: { asOf: string; priorAsOf: string }): string {
  return JSON.stringify(opts)
}

export function loadReportedNetWorth(
  opts: { asOf?: string; priorAsOf?: string },
): Promise<NetWorthPageOut> {
  const q = new URLSearchParams()
  if (opts.asOf) q.set('as_of', opts.asOf)
  if (opts.priorAsOf) q.set('prior_as_of', opts.priorAsOf)
  const qs = q.toString()
  return api<NetWorthPageOut>('reporting/net-worth' + (qs ? '?' + qs : ''))
}

export {
  type CurrencyRegistryEntry, type ReportingPolicySummary, loadCurrencyRegistry, loadReportingPolicy,
} from '@/features/analysis/reportedApi'
