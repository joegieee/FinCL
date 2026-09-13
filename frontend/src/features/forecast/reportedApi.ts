import { api, type components } from '@/lib/api'
import type { ForecastPage } from './api'

// Wire shapes for the reported Forecast workspace (M4 step 14, §3.3, §7 M4).
export type ForecastPageOut = components['schemas']['ForecastPageOut']
export type ReportedForecastCurrencyOut = components['schemas']['ReportedForecastCurrencyOut']
export type ReportedForecastScenarioOut = components['schemas']['ReportedForecastScenarioOut']
export type ReportedMonthRowOut = components['schemas']['ReportedMonthRowOut']
export type ReportedForecastEventOut = components['schemas']['ReportedForecastEventOut']
export type CombinedMinimumOut = components['schemas']['CombinedMinimumOut']
export type ConsolidatedValuationDTO = components['schemas']['ConsolidatedValuationDTO']
export type ValuedMoneyDTO = components['schemas']['ValuedMoneyDTO']

/**
 * The backend deliberately leaves `native` untyped rather than duplicating
 * the already-established `ForecastPage` shape a second time as generated
 * schema (see `api_reporting.py::ForecastPageOut`'s own docstring). Cast it
 * back to that existing type here, at the one call site that reads it.
 */
export function nativeForecast(page: ForecastPageOut): ForecastPage {
  return page.native as unknown as ForecastPage
}

// Forecast has no history range and no record filter of its own (native
// scope: 'none'); the horizon is owner-defined from the supported cash
// cutoff (§7 M4: "A history filter must not change horizon").
export function loadReportedForecast(): Promise<ForecastPageOut> {
  return api<ForecastPageOut>('reporting/forecast')
}

export {
  type CurrencyRegistryEntry, type ReportingPolicySummary, loadCurrencyRegistry, loadReportingPolicy,
} from '@/features/analysis/reportedApi'
