import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, ArrowRight, CheckCircle2, CircleHelp, FileUp, Landmark, ShieldCheck, Wallet } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { money, type Dashboard } from '@/lib/api'
import { loadBaselineEstimate, loadBaselineReadiness, type BaselineEstimate, type BaselineReadiness } from '@/features/baseline/api'
import { loadCashPosition, type CashPosition } from '@/features/cash/api'
import { loadForecast, type ForecastCurrency, type ForecastPage } from '@/features/forecast/api'
import { loadNetWorth, type NetWorthPage } from '@/features/networth/api'
import { loadObligations, type ObligationsPage } from '@/features/obligations/api'
import { loadRecurring, type RecurringPage } from '@/features/planning/api'
import { loadImports, type ImportsPage } from '@/features/setup/api'

type OverviewDestination = 'Setup' | 'Accounts' | 'Baseline' | 'Cash' | 'Forecast' | 'Controls' | 'Cash Flow' | 'Categorization' | 'Planning' | 'Money Owed' | 'Net worth'

type Snapshot = {
  imports?: ImportsPage
  readiness?: BaselineReadiness
  estimate?: BaselineEstimate
  cash?: CashPosition
  forecast?: ForecastPage
  netWorth?: NetWorthPage
  obligations?: ObligationsPage
  recurring?: RecurringPage
}

const unavailable = 'Evidence not available yet'

export function OverviewActionPanel({ data, onOpen, onForecast }: { data: Dashboard; onOpen: (destination: OverviewDestination) => void; onForecast?: (page: ForecastPage | undefined) => void }) {
  const [snapshot, setSnapshot] = useState<Snapshot>({})
  const [loading, setLoading] = useState(true)
  const sequence = useRef(0)

  const load = useCallback(() => {
    const id = ++sequence.current
    setLoading(true)
    void Promise.allSettled([
      loadImports(), loadBaselineReadiness(), loadBaselineEstimate(), loadCashPosition(),
      loadForecast(), loadNetWorth(), loadObligations(), loadRecurring(),
    ]).then(results => {
      if (id !== sequence.current) return
      const value = <T,>(index: number) => results[index].status === 'fulfilled' ? results[index].value as T : undefined
      const forecast = value<ForecastPage>(4)
      setSnapshot({
        imports: value<ImportsPage>(0), readiness: value<BaselineReadiness>(1), estimate: value<BaselineEstimate>(2), cash: value<CashPosition>(3),
        forecast, netWorth: value<NetWorthPage>(5), obligations: value<ObligationsPage>(6), recurring: value<RecurringPage>(7),
      })
      onForecast?.(forecast)
      setLoading(false)
    })
  }, [onForecast])
  useEffect(() => { load() }, [load, data.generation])

  const reserve = reserveText(snapshot.estimate, data.currency)
  const surplus = surplusText(snapshot.estimate, data.currency)
  const cash = cashText(snapshot.cash)
  const owed = moneyOwedText(snapshot.obligations, data.currency)
  const risk = riskText(snapshot.forecast)
  const netWorth = netWorthText(snapshot.netWorth, data.currency)
  const imports = snapshot.imports
  const readiness = snapshot.readiness
  const estimate = snapshot.estimate
  const recurring = snapshot.recurring

  return <div aria-busy={loading} className={loading && Object.keys(snapshot).length ? 'content-loading' : ''}>
    <section className="overview-lead" aria-label="Financial position and next actions">
      <div className="overview-lead-heading"><div><p className="eyebrow">START HERE</p><h2>Your evidence-backed position</h2></div>{loading && <small>Checking ledger evidence…</small>}</div>
      <div className="overview-position-grid">
        <OverviewCard title="Cash" value={cash.value} note={cash.note} icon={<Wallet />} onClick={() => onOpen('Cash')} />
        <OverviewCard title="Reserve" value={reserve.value} note={reserve.note} icon={<ShieldCheck />} onClick={() => onOpen('Baseline')} />
        <OverviewCard title="Monthly surplus" value={surplus.value} note={surplus.note} icon={<Landmark />} onClick={() => onOpen('Baseline')} />
        <OverviewCard title="Money owed" value={owed.value} note={owed.note} icon={<ArrowRight />} onClick={() => onOpen('Money Owed')} />
        <OverviewCard title="Forecast risk" value={risk.value} note={risk.note} icon={<AlertTriangle />} onClick={() => onOpen('Forecast')} warning={risk.warning} />
      </div>
      <div className="overview-destinations">
        <Button variant="outline" onClick={() => onOpen('Forecast')}>Open forecast <ArrowRight size={14} /></Button>
        <Button variant="outline" onClick={() => onOpen('Money Owed')}>Money owed <ArrowRight size={14} /></Button>
        <Button variant="outline" onClick={() => onOpen('Cash Flow')}>Spending analysis / cash flow <ArrowRight size={14} /></Button>
        <Button variant="outline" onClick={() => onOpen('Net worth')}>Tracked net worth: {netWorth} <ArrowRight size={14} /></Button>
      </div>
    </section>

    <Card className="setup-checklist">
      <div className="panel-title"><div><p className="eyebrow">SETUP CHECKLIST</p><h2>Build the picture from your records</h2></div></div>
      <ol>
        <ChecklistItem done={!!imports && imports.imports.length > 0} title="Import statements" detail={!imports ? unavailable : imports.imports.length === 0 ? 'No statements imported.' : `${imports.imports.length} import record${imports.imports.length === 1 ? '' : 's'} retained.`} onOpen={() => onOpen('Setup')} />
        <ChecklistItem done={!!readiness && readiness.pockets.length > 0 && readiness.tasks.length === 0} title="Inspect account and month readiness" detail={!readiness ? unavailable : readiness.pockets.length === 0 ? 'No account or month evidence yet.' : readiness.tasks.length ? `${readiness.tasks.length} readiness item${readiness.tasks.length === 1 ? '' : 's'} still need attention.` : `${readiness.pockets.length} pocket${readiness.pockets.length === 1 ? '' : 's'} inspected.`} onOpen={() => onOpen(readiness?.tasks.some(task => task.kind === 'import-missing') ? 'Setup' : 'Baseline')} />
        <ChecklistItem done={data.review_count === 0 && !!recurring && recurring.candidates.length === 0} title="Review detected classifications and commitments" detail={!recurring ? unavailable : `${data.review_count} classification${data.review_count === 1 ? '' : 's'} to review · ${recurring.candidates.length} detected commitment candidate${recurring.candidates.length === 1 ? '' : 's'}.`} onOpen={() => onOpen(data.review_count > 0 ? 'Categorization' : 'Planning')} />
        <ChecklistItem done={estimate?.available === true && snapshot.cash?.current_position_supported === true} title="Inspect derived baseline and evidenced cash" detail={baselineAndCashText(estimate, snapshot.cash)} onOpen={() => onOpen(estimate?.available ? 'Cash' : 'Baseline')} />
        <ChecklistItem done={reserve.ready} title="Choose a reserve policy" detail={reserve.policyNote} onOpen={() => onOpen('Controls')} />
        <ChecklistItem done={forecastReady(snapshot.forecast)} title="Open forecast" detail={risk.detail} onOpen={() => onOpen('Forecast')} />
      </ol>
      <p className="muted-note">The checklist never substitutes a typed spending estimate for statement evidence. A zero is shown only when the ledger actually observed one.</p>
    </Card>
  </div>
}

function OverviewCard({ title, value, note, icon, onClick, warning = false }: { title: string; value: string; note: string; icon: React.ReactNode; onClick: () => void; warning?: boolean }) {
  return <button className={`overview-position${warning ? ' is-warning' : ''}`} onClick={onClick}><span>{title}{icon}</span><strong>{value}</strong><small>{note}</small></button>
}

function ChecklistItem({ done, title, detail, onOpen }: { done: boolean; title: string; detail: string; onOpen: () => void }) {
  return <li><span className={`checklist-state ${done ? 'is-done' : ''}`}>{done ? <CheckCircle2 size={16} /> : <CircleHelp size={16} />}</span><div><strong>{title}</strong><p>{detail}</p></div><Button variant="ghost" size="sm" onClick={onOpen}>Open <ArrowRight size={13} /></Button></li>
}

function reserveText(estimate: BaselineEstimate | undefined, currency: string) {
  if (!estimate) return { value: unavailable, note: 'Checking baseline evidence.', policyNote: 'Reserve evidence is still being checked.', ready: false }
  if (!estimate.available) return { value: 'Not derivable', note: estimate.reason, policyNote: 'A reserve policy cannot be grounded until the baseline is derived.', ready: false }
  const reserve = estimate.by_currency[currency]?.reserve
  if (!reserve) return { value: unavailable, note: `No ${currency} reserve read.`, policyNote: 'No reserve read is available for this currency.', ready: false }
  if (!reserve.available) return { value: 'Not derivable', note: reserve.reason, policyNote: reserve.reason, ready: false }
  return { value: money(reserve.amount, currency), note: reserve.basis, policyNote: `Evidence-derived minimum: ${money(reserve.amount, currency)}. Review how it is used in Controls.`, ready: true }
}

function surplusText(estimate: BaselineEstimate | undefined, currency: string) {
  if (!estimate) return { value: unavailable, note: 'Checking baseline evidence.' }
  if (!estimate.available) return { value: 'Not derivable', note: estimate.reason }
  const value = estimate.by_currency[currency]?.historical_surplus_monthly
  return value === undefined ? { value: unavailable, note: `No ${currency} baseline read.` } : { value: money(value, currency), note: 'Historical monthly surplus from the derived baseline.' }
}

function cashText(cash: CashPosition | undefined) {
  if (!cash) return { value: unavailable, note: 'Checking liquid-cash evidence.' }
  if (!cash.current_position_supported) return { value: 'Not observed today', note: cash.current_position_reasons[0] ?? 'Current cash is modelled, not observed.' }
  const values = Object.entries(cash.liquid_total)
  return values.length ? { value: values.map(([currency, value]) => money(value, currency)).join(' · '), note: `Observed as of ${cash.generated_for}.` } : { value: 'No liquid cash recorded', note: `Observed as of ${cash.generated_for}.` }
}

function moneyOwedText(page: ObligationsPage | undefined, currency: string) {
  if (!page) return { value: unavailable, note: 'Checking obligations.' }
  const lanes = page.totals_by_currency[currency]
  const unresolved = page.obligations.filter(item => item.amount_basis === 'unknown').length
  if (!lanes) return { value: unresolved ? 'Amount unresolved' : 'No countable amount', note: unresolved ? `${unresolved} obligation${unresolved === 1 ? '' : 's'} lacks an evidenced amount.` : 'No outflow obligation has a countable remaining amount.' }
  return { value: `${money(lanes.bank_withdrawal, currency)} bank · ${money(lanes.via_card_statement, currency)} card`, note: unresolved ? `${unresolved} unresolved obligation${unresolved === 1 ? '' : 's'} excluded from these totals.` : 'Remaining amounts by cash effect.' }
}

function riskText(page: ForecastPage | undefined) {
  if (!page) return { value: unavailable, note: 'Checking the forecast.', detail: 'Forecast inputs are still being checked.', warning: false }
  if (!page.available) return { value: 'Forecast unavailable', note: page.reason ?? 'No supported forecast input.', detail: page.reason ?? 'No supported forecast input.', warning: true }
  const usable = Object.values(page.by_currency).filter((item): item is ForecastCurrency => item.available)
  if (!usable.length) return { value: 'Forecast unavailable', note: page.reason ?? 'No currency has a supported forecast.', detail: page.reason ?? 'No currency has a supported forecast.', warning: true }
  const breaches = usable.filter(item => item.base.first_zero.occurs || item.conservative.first_zero.occurs || (item.base.first_reserve_breach.available && item.base.first_reserve_breach.occurs) || (item.conservative.first_reserve_breach.available && item.conservative.first_reserve_breach.occurs))
  return breaches.length ? { value: 'Breach flagged', note: `${breaches.length} forecast currency path${breaches.length === 1 ? '' : 's'} reaches a cash or reserve breach.`, detail: 'Review the flagged cash or reserve breach in the forecast.', warning: true } : { value: 'No breach flagged', note: 'Within the supported forecast horizon.', detail: 'The supported horizon currently has no cash or reserve breach flagged.', warning: false }
}

function netWorthText(page: NetWorthPage | undefined, currency: string) {
  if (!page) return unavailable
  const summary = page.per_currency[currency]
  return summary ? money(summary.tracked_net_worth, currency) : 'No tracked subtotal'
}

function baselineAndCashText(estimate: BaselineEstimate | undefined, cash: CashPosition | undefined) {
  if (!estimate || !cash) return unavailable
  if (!estimate.available && !cash.current_position_supported) return `${estimate.reason} Cash is not observed today.`
  if (!estimate.available) return estimate.reason
  if (!cash.current_position_supported) return cash.current_position_reasons[0] ?? 'Cash is not observed today.'
  return 'Derived baseline and observed current cash are available.'
}

function forecastReady(page: ForecastPage | undefined) {
  return !!page?.available && Object.values(page.by_currency).some(item => item.available)
}
