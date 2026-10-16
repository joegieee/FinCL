import { useState } from 'react'
import { MoneyBarChart } from '@/components/MoneyBarChart'
import { reportedChartAmount, workspaceReportingCurrency } from '@/lib/workspaceReporting'
import { COLUMN_LABEL, type Column, type ReportResult } from './api'

export function ReportChart({ result, columns }: { result: ReportResult; columns: Column[] }) {
  const metrics = columns.filter(column => column !== 'entry_count')
  const [chosen, setChosen] = useState<Exclude<Column, 'entry_count'>>('net_cost')
  const metric = metrics.includes(chosen) ? chosen : metrics[0]
  if (!metric || result.rows.length === 0) return null
  const reportingCurrency = workspaceReportingCurrency(result)
  const currencies = reportingCurrency ? [reportingCurrency] : [...new Set(result.rows.map(row => row.currency))].sort()
  return <div className="report-charts">
    <label>Chart metric<select value={metric} onChange={event => setChosen(event.target.value as Exclude<Column, 'entry_count'>)}>
      {metrics.map(column => <option key={column} value={column}>{COLUMN_LABEL[column]}</option>)}
    </select></label>
    {currencies.map(currency => {
      const rows = result.rows.filter(row => reportingCurrency || row.currency === currency)
      return <MoneyBarChart key={currency} title={`${COLUMN_LABEL[metric]} by report group`} currency={currency} showTable={false}
        caption={`${result.window.start ?? 'All time'}${result.window.end ? ` – ${result.window.end}` : ''} · recorded flow · ${rows.length} groups`}
        rows={rows.map(row => {
          const amount = reportedChartAmount(row, metric, currency)
          return { label: row.group.map(group => group.label).join(' / ') || 'All entries', value: amount.currency === currency ? amount.value : null,
            detail: `${COLUMN_LABEL[metric]} · native ${row.currency}` }
        })}
        footer="Each bar is one report row, in table order. Missing conversions leave gaps. Scroll sideways for more groups; the report table below keeps every exact figure." />
    })}
  </div>
}
