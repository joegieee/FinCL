import { MoneyBarChart, type MoneyBar } from '@/components/MoneyBarChart'
import type { ConsolidatedValuationDTO, NetWorthPageOut } from './reportedApi'
import { reasonText } from '@/lib/limitations'
import { dateLabel } from '@/lib/format'

function bar(label: string, cell: ConsolidatedValuationDTO): MoneyBar {
  return {
    label, value: cell.total?.value ?? null,
    detail: cell.total ? 'Reported tracked subtotal' : cell.unavailable_reasons.map(reasonText).join(' ') || 'Incomplete valuation; see components below',
  }
}

export function NetWorthCharts({ page }: { page: NetWorthPageOut }) {
  return <>
    <MoneyBarChart title="Assets, liabilities and the balance" currency={page.reporting_currency}
      caption={`Tracked positions as of ${dateLabel(page.as_of)} · manual values excluded`}
      rows={[bar('Assets', page.totals.assets), bar('Liabilities', page.totals.liabilities), bar('Net worth', page.totals.net_worth)]}
      footer="Liabilities are shown as amounts owed. Missing valuations leave gaps; partial subtotals are not plotted. This covers tracked positions only." />
    {page.trend && <MoneyBarChart title="How the tracked balance changed" currency={page.reporting_currency}
      caption={`${dateLabel(page.trend.prior_as_of)} → ${dateLabel(page.trend.current_as_of)} · two dated observations`}
      rows={[bar(page.trend.prior_as_of, page.trend.prior_net_worth), bar(page.trend.current_as_of, page.trend.current_net_worth)]}
      footer={page.trend.scope_changed ? 'Scope changed: these observations contain different components. The change is not solely a gain or loss.' : 'Bars show two observations only; no balances are inferred between dates. Missing valuations leave gaps.'} />}
  </>
}
