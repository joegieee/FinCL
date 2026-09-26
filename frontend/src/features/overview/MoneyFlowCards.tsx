import { ArrowRight } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { ConsolidatedMoney } from '@/components/Money'
import { dateLabel } from '@/lib/format'
import { readerLimitations } from '@/lib/limitations'
import type { ConsolidatedValuationDTO, MoneyFlowOut } from './reportedApi'

export type OverviewDestination = 'Analysis' | 'Transactions' | 'Cash' | 'Net worth' | 'Forecast' | 'Income'

function Cv({ result, currency }: { result: ConsolidatedValuationDTO; currency: string }) {
  return <ConsolidatedMoney
    total={result.total} partialTotal={result.partial_total} currency={currency}
    unavailableReasons={result.unavailable_reasons} excludedDetail={result.excluded_detail}
  />
}

function hasAmount(result: ConsolidatedValuationDTO): boolean {
  return result.total != null || result.partial_total != null
}

/**
 * The four Overview summary cards (M3b.3 step 10, plan §3.3-3.4, §6.3). Every
 * figure comes from one `GET /api/v1/money-flow` read over *all* pockets by
 * default; this component performs no arithmetic of its own — it only lays
 * out the DTOs `MoneyFlowOut` already carries.
 */
export function MoneyFlowCards({ page, onOpen }: { page: MoneyFlowOut; onOpen: (destination: OverviewDestination) => void }) {
  const currency = page.reporting_currency
  const flow = page.summary.economic_flow
  const burn = page.summary.gross_burn
  const cash = page.summary.cash_at_cutoff
  const forecast = page.summary.forecast_closing
  const limitations = readerLimitations(page.limitations)

  return (
    <div className="money-flow-section">
      <div className="comparison-grid money-flow-grid">
        <Card className="comparison-card money-flow-card">
          <span className="comparison-label">Economic flow</span>
          <div className="money-flow-row"><span>In</span><Cv result={flow.inflow} currency={currency} /></div>
          <div className="money-flow-row"><span>Out</span><Cv result={flow.outflow} currency={currency} /></div>
          <div className="money-flow-row money-flow-net"><span>Net</span><Cv result={flow.net} currency={currency} /></div>
          {hasAmount(flow.exceptional_adjustments) && (
            <small>Exceptional adjustments disclosed separately: <Cv result={flow.exceptional_adjustments} currency={currency} /></small>
          )}
          <button className="linkish" onClick={() => onOpen('Analysis')}>Open Analysis <ArrowRight size={13} /></button>
        </Card>

        <Card className="comparison-card money-flow-card">
          <span className="comparison-label">Average monthly spending + fees</span>
          <strong>
            {burn.amount
              ? <Cv result={burn.amount} currency={currency} />
              : <span className="money money-unavailable"><span aria-hidden="true">—</span><span className="money-unavailable-detail"><strong>Not available</strong>{burn.reason && <span>{burn.reason}</span>}</span></span>}
          </strong>
          <small>
            {burn.month_count} complete covered month{burn.month_count === 1 ? '' : 's'}
            {burn.included_months.length ? `: ${burn.included_months.join(', ')}` : ''}.
          </small>
          {burn.excluded_months.length > 0 && <small>No evidence for: {burn.excluded_months.join(', ')}.</small>}
          <button className="linkish" onClick={() => onOpen('Analysis')}>Open Analysis <ArrowRight size={13} /></button>
        </Card>

        <Card className="comparison-card money-flow-card">
          <span className="comparison-label">Supported cash at cutoff</span>
          <strong><Cv result={cash.amount} currency={currency} /></strong>
          <small>Balance as of {cash.balance_as_of ? dateLabel(cash.balance_as_of) : 'an unknown date'} · valued as of {dateLabel(cash.valuation_as_of)}.</small>
          <button className="linkish" onClick={() => onOpen('Cash')}>Open Cash <ArrowRight size={13} /></button>
        </Card>

        <Card className="comparison-card money-flow-card">
          <span className="comparison-label">Six-month forecast closing</span>
          <div className="money-flow-row"><span>Base</span><Cv result={forecast.base} currency={currency} /></div>
          <div className="money-flow-row"><span>Conservative</span><Cv result={forecast.conservative} currency={currency} /></div>
          <small>
            Opening {forecast.opening_balance_as_of ? dateLabel(forecast.opening_balance_as_of) : 'an unknown date'} · rates held as of {dateLabel(forecast.valuation_as_of)}
            {forecast.horizon_end ? `, through ${dateLabel(forecast.horizon_end)}` : ''}.
          </small>
          <button className="linkish" onClick={() => onOpen('Forecast')}>Open Forecast <ArrowRight size={13} /></button>
        </Card>
      </div>

      {limitations.length > 0 && (
        <div className="alert" role="status">
          <div><strong>Some metrics above are limited.</strong><ul>{limitations.map(text => <li key={text}>{text}</li>)}</ul></div>
        </div>
      )}

      <div className="overview-destinations">
        <Button variant="outline" onClick={() => onOpen('Analysis')}>Analysis <ArrowRight size={14} /></Button>
        <Button variant="outline" onClick={() => onOpen('Transactions')}>Transactions <ArrowRight size={14} /></Button>
        <Button variant="outline" onClick={() => onOpen('Cash')}>Cash <ArrowRight size={14} /></Button>
        <Button variant="outline" onClick={() => onOpen('Net worth')}>Net worth <ArrowRight size={14} /></Button>
        <Button variant="outline" onClick={() => onOpen('Forecast')}>Forecast <ArrowRight size={14} /></Button>
        <Button variant="outline" onClick={() => onOpen('Income')}>Income <ArrowRight size={14} /></Button>
      </div>
    </div>
  )
}
