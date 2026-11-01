import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { AlertTriangle, ArrowRight, ChevronRight } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { ConsolidatedMoney } from '@/components/Money'
import { money, shortCategory } from '@/lib/api'
import { emptyFilters, loadReportedAnalysis, type ReportedAnalysis } from '@/features/analysis/reportedApi'
import { loadForecast, type ForecastPage } from '@/features/forecast/api'
import { Bars, Panel, plotted } from '@/features/overview/Panel'
import { loadSufficiency, type SufficiencyPage } from '@/features/sufficiency/api'
import { when } from '@/features/sufficiency'
import { CashTimeline } from './CashTimeline'
import { lastFullMonth, loadJourney, type MoneyJourney, type OwedGroup } from './api'

export type JourneyDestination = 'Setup' | 'Analysis' | 'Obligations' | 'Debts' | 'Will I have enough' | 'Categorization'

const monthName = (iso: string) => new Date(iso + 'T12:00:00Z').toLocaleDateString('en-SG', { month: 'long', year: 'numeric', timeZone: 'UTC' })

const OWED_COLOR: Record<OwedGroup['origin'], string> = {
  personal_debt: 'var(--chart-1)',
  card_statement: 'var(--chart-3)',
  installment_plan: 'var(--chart-5)',
  recurring_commitment: 'var(--chart-6)',
}

/**
 * The answer first, then what it rests on. Every amount is worked out on the
 * server; this view only lays them out. `cash` and `children` are the parts
 * that come from the Overview's own money-flow read.
 */
export function MoneyJourneyPanel({ generation, onOpen, cash, needs = [], children }: {
  generation: string
  onOpen: (destination: JourneyDestination) => void
  cash?: ReactNode
  needs?: { label: string; open: JourneyDestination }[]
  children?: ReactNode
}) {
  const [journey, setJourney] = useState<MoneyJourney | null>(null)
  const [journeyError, setJourneyError] = useState('')
  const [spending, setSpending] = useState<ReportedAnalysis | null>(null)
  const [sufficiency, setSufficiency] = useState<SufficiencyPage | null>(null)
  const [sufficiencyError, setSufficiencyError] = useState('')
  const [forecast, setForecast] = useState<ForecastPage | null>(null)

  const load = useCallback(() => {
    setJourneyError(''); setSufficiencyError('')
    loadJourney().then(page => {
      setJourney(page)
      const through = page.statements.complete_through
      if (through) {
        const [start, end] = lastFullMonth(through)
        loadReportedAnalysis({ historyStart: start, historyEnd: end, filters: { ...emptyFilters, comparison: 'none' } })
          .then(setSpending).catch(() => setSpending(null))
      }
    }).catch(e => setJourneyError((e as Error).message))
    // The slowest reads: they run the whole forecast, so they fill in last.
    loadSufficiency().then(setSufficiency).catch(e => setSufficiencyError((e as Error).message))
    loadForecast().then(setForecast).catch(() => setForecast(null))
  }, [])
  useEffect(() => { load() }, [load, generation])

  const today = journey?.generated_for ?? new Date().toISOString().slice(0, 10)
  const currency = journey?.reporting_currency ?? 'SGD'
  const owed = journey?.owed.groups.filter(group => group.count > 0) ?? []
  const uncounted = journey ? journey.owed.unknown_amount_count + journey.owed.unconverted_count : 0
  const spent = spending?.summary.spending?.current
  const behind = journey?.statements.accounts.filter(account => account.behind) ?? []
  const attention = [
    ...behind.map(account => ({ label: `${account.name} statements are ${account.days_behind} days behind`, open: 'Setup' as const })),
    ...needs,
  ]

  return (
    <section className="money-journey" aria-label="Your money">
      {journeyError && <p className="form-error" role="alert">{journeyError}</p>}
      <div className="ov-band">
        <div className="ov-answer">
          <h2>Will you have enough?</h2>
          <ShortAnswer page={sufficiency} error={sufficiencyError} today={today} />
          <Button variant="ghost" size="sm" onClick={() => onOpen('Will I have enough')}>See why <ArrowRight size={14} /></Button>
        </div>
        {cash}
        <div className="ov-stat">
          <button className="linkish" onClick={() => onOpen('Analysis')}>{journey?.statements.complete_through ? `Spent in ${monthName(lastFullMonth(journey.statements.complete_through)[0])}` : 'Spent'} <ArrowRight size={13} /></button>
          {spent ? <strong><ConsolidatedMoney total={spent.total} partialTotal={spent.partial_total} currency={spending.reporting_currency} unavailableReasons={spent.unavailable_reasons} excludedDetail={spent.excluded_detail} /></strong> : <span className="ov-note">{journey?.statements.complete_through ? 'Adding up…' : 'Appears once statements are in.'}</span>}
        </div>
        <div className="ov-stat">
          <button className="linkish" onClick={() => onOpen('Obligations')}>To pay <ArrowRight size={13} /></button>
          {journey ? <strong>{money(journey.owed.total, currency)}</strong> : <span className="ov-note">Checking…</span>}
        </div>
      </div>
      <div className={attention.length && forecast?.available ? 'ov-chart-row' : undefined}>
        {forecast?.available && <CashTimeline page={forecast} today={today} reportingCurrency={currency} />}
        {attention.length > 0 && <section className="ov-needs" aria-label="Needs you">
          <h3>Needs you</h3>
          <ul>{attention.map(item => <li key={item.label}><button onClick={() => onOpen(item.open)}><span>{item.label}</span><ChevronRight size={16} aria-hidden="true" /></button></li>)}</ul>
        </section>}
      </div>

      <div className="ov-panels">
        <Panel title="Statements" onOpen={() => onOpen('Setup')}>
          {!journey ? <p className="muted-note">Checking…</p>
            : journey.statements.accounts.length === 0 ? <p className="muted-note">Upload statements to begin.</p>
            : <>
              <p className="ov-lead"><strong>{when(journey.statements.complete_through, today)}</strong><small>latest complete date</small></p>
              <Bars rows={[...journey.statements.accounts].sort((a, b) => b.days_behind - a.days_behind).map(account => ({
                key: account.account_id, label: account.name, size: account.days_behind, value: `${account.days_behind}d`,
                className: account.behind ? 'is-behind' : undefined,
              }))} />
              <p className="ov-note">Days since the last statement</p>
            </>}
        </Panel>

        <Panel title="Spent" onOpen={() => onOpen('Analysis')}>
          {!journey?.statements.complete_through ? <p className="muted-note">Appears once statements are in.</p>
            : !spending ? <p className="muted-note">Adding up…</p>
            : <Bars rows={spending.category_ranking.slice(0, 5).map(row => ({
                key: row.name, label: shortCategory(row.name), size: plotted(row.result),
                value: <ConsolidatedMoney total={row.result.total} partialTotal={row.result.partial_total} currency={spending.reporting_currency}
                  unavailableReasons={row.result.unavailable_reasons} excludedDetail={row.result.excluded_detail} />,
              }))} />}
        </Panel>

        <Panel title="Owed" onOpen={() => onOpen('Obligations')}>
          {!journey ? <p className="muted-note">Checking…</p> : <>
            <div className="ov-stack" aria-hidden="true">
              {owed.filter(group => Number(group.total) > 0).map(group => <i key={group.origin} style={{ flexGrow: Number(group.total), background: OWED_COLOR[group.origin] }} />)}
            </div>
            <ul className="ov-key">
              {owed.map(group => <li key={group.origin}><i style={{ background: OWED_COLOR[group.origin] }} /><span>{group.label}</span><span>{money(group.total, currency)}</span></li>)}
            </ul>
            {uncounted > 0 && <p className="ov-note">{uncounted} more with no known amount or rate</p>}
            {journey.owed.owed_to_you.count > 0 && <p className="ov-note">Owed to you: {money(journey.owed.owed_to_you.total, currency)}</p>}
          </>}
        </Panel>

        {children}
      </div>
    </section>
  )
}

function ShortAnswer({ page, error, today }: { page: SufficiencyPage | null; error: string; today: string }) {
  if (error) return <p className="form-error">{error}</p>
  if (!page) return <p className="ov-verdict is-waiting">Working it out…</p>
  const answer = page.combined_answer
  if (!answer || !answer.pockets.some(p => p.counted === 'projected' || p.counted === 'held_as_is')) {
    return <p className="ov-verdict is-waiting">Upload statements first</p>
  }
  const c = answer.reporting_currency
  const base = answer.base
  const missing = answer.not_in_the_path
  const shortIfRepaid = missing?.short_if_repaid_now ?? null
  return <>
    {base.verdict === 'short' && <><p className="ov-verdict is-short">No — short by {money(base.shortfall, c)}</p><small>around {when(base.shortfall_on, today)}</small></>}
    {base.verdict === 'below_reserve' && <><p className="ov-verdict">Just — you dip into your reserve</p><small>by up to {money(base.shortfall, c)}</small></>}
    {base.verdict === 'enough' && <><p className={`ov-verdict${shortIfRepaid ? ' is-short' : ''}`}>{shortIfRepaid ? 'Only if your debts can wait' : answer.incomplete_because?.length ? answer.answer : 'Yes'}</p><small>Lowest point {money(base.lowest, c)}</small></>}
    {shortIfRepaid && missing && (
      <p className="ov-warning" role="note">
        <AlertTriangle size={14} aria-hidden="true" /> {money(missing.total, c)} you owe has no due date. Repaying it now leaves you <strong>{money(shortIfRepaid, c)} short</strong>.
      </p>
    )}
  </>
}
