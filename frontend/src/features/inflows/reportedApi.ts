import { api, type components } from '@/lib/api'
import type { CandidatesPage } from './api'

// Wire shapes for the reported Income (Inflows) workspace (M4 step 15, §3.1).
export type IncomeCandidatesPageOut = components['schemas']['IncomeCandidatesPageOut']
export type ReportedIncomeCandidateOut = components['schemas']['ReportedIncomeCandidateOut']
export type ReportedOccurrenceOut = components['schemas']['ReportedOccurrenceOut']
export type ReportedVariationOut = components['schemas']['ReportedVariationOut']
export type ValuedMoneyDTO = components['schemas']['ValuedMoneyDTO']

/**
 * The backend deliberately leaves `native` untyped rather than duplicating
 * the already-established `CandidatesPage` shape a second time as generated
 * schema (see `api_reporting.py::IncomeCandidatesPageOut`'s own docstring).
 * Cast it back to that existing type here, at the one call site that reads it.
 */
export function nativeCandidates(page: IncomeCandidatesPageOut): CandidatesPage {
  return page.native as unknown as CandidatesPage
}

// The queue has no history range and no record filter of its own (native
// scope: 'none'); its scope is unresolved income work across the whole
// ledger, never the Overview history range (§7 M4).
export function loadReportedCandidates(): Promise<IncomeCandidatesPageOut> {
  return api<IncomeCandidatesPageOut>('reporting/inflows/candidates')
}

export {
  type CurrencyRegistryEntry, type ReportingPolicySummary, loadCurrencyRegistry, loadReportingPolicy,
} from '@/features/analysis/reportedApi'
