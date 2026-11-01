import type { ReactNode } from 'react'
import { ConsolidatedMoney } from '@/components/Money'
import { Bars, Panel, plotted } from './Panel'
import type { ConsolidatedValuationDTO, MoneyFlowOut } from './reportedApi'

export function Cv({ result, currency }: { result: ConsolidatedValuationDTO; currency: string }) {
  return <ConsolidatedMoney
    total={result.total} partialTotal={result.partial_total} currency={currency}
    unavailableReasons={result.unavailable_reasons} excludedDetail={result.excluded_detail}
  />
}

/** Every figure comes from one `GET /api/v1/money-flow` read; nothing is added up here. */
export function MoneyFlowPanel({ page, period, covered = true, onOpen }: { page: MoneyFlowOut; period?: ReactNode; covered?: boolean; onOpen: () => void }) {
  const currency = page.reporting_currency
  const flow = page.summary.economic_flow
  const burn = page.summary.gross_burn

  return <Panel title="In and out" className="ov-panel-flow" action={period} onOpen={onOpen}>
    {!covered && <p className="ov-note" role="status"><strong>This period has missing coverage.</strong> Choose a covered period or import the missing statements.</p>}
    <p className="ov-lead"><strong><Cv result={flow.net} currency={currency} /></strong><small>net</small></p>
    <Bars rows={[
      { key: 'in', label: 'In', size: plotted(flow.inflow), value: <Cv result={flow.inflow} currency={currency} /> },
      { key: 'out', label: 'Out, with fees', size: plotted(flow.outflow), value: <Cv result={flow.outflow} currency={currency} /> },
    ]} />
    {!!plotted(flow.exceptional_adjustments) && <p className="ov-note">Exceptional adjustments, kept apart: <Cv result={flow.exceptional_adjustments} currency={currency} /></p>}
    <p className="ov-note">Monthly spending{burn.month_count ? `, ${burn.month_count} month average` : ''}: {burn.amount ? <Cv result={burn.amount} currency={currency} /> : burn.reason ?? 'not available'}</p>
  </Panel>
}
