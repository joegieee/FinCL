import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, ArrowRight, CheckCircle2, Upload } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { ConsolidatedMoney } from '@/components/Money'
import { money, shortCategory } from '@/lib/api'
import { dateLabel } from '@/lib/format'
import { emptyFilters, loadReportedAnalysis, type ReportedAnalysis } from '@/features/analysis/reportedApi'
import { loadSufficiency, type SufficiencyPage } from '@/features/sufficiency/api'
import { when } from '@/features/sufficiency'
import { lastFullMonth, loadJourney, type MoneyJourney } from './api'

export type JourneyDestination = 'Setup' | 'Analysis' | 'Obligations' | 'Debts' | 'Will I have enough'

const monthName = (iso: string) => new Date(iso + 'T12:00:00Z').toLocaleDateString('en-SG', { month: 'long', year: 'numeric', timeZone: 'UTC' })

/**
 * The four questions, in order: are my statements in, what did I spend on,
 * what do I owe, and how short will I be. Each step is a short answer and a
 * way into the page that has the detail. Every amount is worked out on the
 * server; this view only lays them out.
 */
export function MoneyJourneyPanel({ generation, onOpen }: { generation: string; onOpen: (destination: JourneyDestination) => void }) {
  const [journey, setJourney] = useState<MoneyJourney | null>(null)
  const [journeyError, setJourneyError] = useState('')
  const [spending, setSpending] = useState<ReportedAnalysis | null>(null)
  const [sufficiency, setSufficiency] = useState<SufficiencyPage | null>(null)
  const [sufficiencyError, setSufficiencyError] = useState('')

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
    // The slowest read: it runs the whole forecast, so it fills in last.
    loadSufficiency().then(setSufficiency).catch(e => setSufficiencyError((e as Error).message))
  }, [])
  useEffect(() => { load() }, [load, generation])

  const today = journey?.generated_for ?? new Date().toISOString().slice(0, 10)
  const currency = journey?.reporting_currency ?? 'SGD'

  return (
    <section className="money-journey" aria-label="Your money in four steps">
      <div className="overview-lead-heading"><div><p className="eyebrow">YOUR MONEY IN FOUR STEPS</p><h2>Statements in, spending, what you owe, and whether it’s enough</h2></div></div>
      {journeyError && <p className="form-error" role="alert">{journeyError}</p>}
      <ol className="journey-steps">
        <Step n={1} title="Your statements" onOpen={() => onOpen('Setup')} action={<><Upload size={14} /> Upload statements</>}>
          {!journey ? <p className="muted-note">Checking your statements…</p>
            : journey.statements.accounts.length === 0 ? <p>No statements yet. Upload the PDFs from your banks; each one’s bank is recognised automatically.</p>
            : <>
              <p className="journey-lead">
                Complete up to <strong>{when(journey.statements.complete_through, today)}</strong>
              </p>
              <ul className="journey-list">
                {journey.statements.accounts.map(account => (
                  <li key={account.account_id} className={account.behind ? 'is-behind' : ''}>
                    <span>{account.behind ? <AlertTriangle size={13} /> : <CheckCircle2 size={13} />} {account.name}</span>
                    <span>to {dateLabel(account.through)}</span>
                  </li>
                ))}
              </ul>
              {journey.statements.behind_count > 0 && <p className="muted-note">Statements more than {journey.statements.stale_after_days} days old hold everything back: upload the newer ones.</p>}
            </>}
        </Step>

        <Step n={2} title="What you spent" onOpen={() => onOpen('Analysis')} action="See all spending">
          {!journey?.statements.complete_through ? <p className="muted-note">Appears once statements are in.</p>
            : !spending ? <p className="muted-note">Adding up your spending…</p>
            : <>
              <p className="journey-lead">
                {monthName(lastFullMonth(journey.statements.complete_through)[0])}: <strong><ConsolidatedMoney
                  total={spending.summary.spending?.current.total ?? null} partialTotal={spending.summary.spending?.current.partial_total ?? null}
                  currency={spending.reporting_currency} unavailableReasons={spending.summary.spending?.current.unavailable_reasons}
                  excludedDetail={spending.summary.spending?.current.excluded_detail} /></strong>
              </p>
              <ul className="journey-list">
                {spending.category_ranking.slice(0, 5).map(row => (
                  <li key={row.name}><span>{shortCategory(row.name)}</span><span><ConsolidatedMoney
                    total={row.result.total} partialTotal={row.result.partial_total} currency={spending.reporting_currency}
                    unavailableReasons={row.result.unavailable_reasons} excludedDetail={row.result.excluded_detail} /></span></li>
                ))}
              </ul>
              <p className="muted-note">The last full month your statements cover.</p>
            </>}
        </Step>

        <Step n={3} title="What you owe" onOpen={() => onOpen('Obligations')} action="See everything owed">
          {!journey ? <p className="muted-note">Checking what you owe…</p> : <>
            <p className="journey-lead"><strong>{money(journey.owed.total, currency)}</strong> still to pay</p>
            <ul className="journey-list">
              {journey.owed.groups.filter(group => group.count > 0).map(group => (
                <li key={group.origin}>
                  <span>{group.label} <small>· {group.count}</small></span>
                  <span>{money(group.total, currency)}</span>
                </li>
              ))}
            </ul>
            {journey.owed.unknown_amount_count + journey.owed.unconverted_count > 0 && (
              <p className="muted-note">{journey.owed.unknown_amount_count + journey.owed.unconverted_count} more without a known amount or rate, not in the total.</p>
            )}
            {journey.owed.owed_to_you.count > 0 && <p className="muted-note">Others owe you {money(journey.owed.owed_to_you.total, currency)} — kept separate, never netted.</p>}
          </>}
        </Step>

        <Step n={4} title="Will you have enough?" onOpen={() => onOpen('Will I have enough')} action="See why">
          <ShortAnswer page={sufficiency} error={sufficiencyError} today={today} />
        </Step>
      </ol>
    </section>
  )
}

function Step({ n, title, onOpen, action, children }: { n: number; title: string; onOpen: () => void; action: React.ReactNode; children: React.ReactNode }) {
  return (
    <li>
      <Card className="journey-step">
        <div className="journey-step-head"><span className="journey-number" aria-hidden="true">{n}</span><h3>{title}</h3></div>
        <div className="journey-body">{children}</div>
        <Button variant="outline" size="sm" onClick={onOpen}>{action} <ArrowRight size={14} /></Button>
      </Card>
    </li>
  )
}

/** The plain answer to "how short am I", including what the forecast can't place. */
function ShortAnswer({ page, error, today }: { page: SufficiencyPage | null; error: string; today: string }) {
  if (error) return <p className="form-error">{error}</p>
  if (!page) return <p className="muted-note">Working it out from your income, spending and bills… this can take a little while.</p>
  const answer = page.combined_answer
  if (!answer || !answer.pockets.some(p => p.counted === 'projected' || p.counted === 'held_as_is')) {
    return <p>Not enough to go on yet. Upload statements first.</p>
  }
  const c = answer.reporting_currency
  const base = answer.base
  const missing = answer.not_in_the_path
  const shortIfRepaid = missing?.short_if_repaid_now && Number(missing.short_if_repaid_now) > 0 ? missing.short_if_repaid_now : null
  return <>
    {base.verdict === 'short' && <p className="journey-lead is-short"><strong>No — short by {money(base.shortfall, c)}</strong><small> around {when(base.shortfall_on, today)}</small></p>}
    {base.verdict === 'below_reserve' && <p className="journey-lead"><strong>Just — you dip into your reserve</strong><small> by up to {money(base.shortfall, c)}</small></p>}
    {base.verdict === 'enough' && <p className={`journey-lead${shortIfRepaid ? ' is-short' : ''}`}><strong>{shortIfRepaid ? 'Only if your debts can wait' : answer.incomplete_because?.length ? answer.answer : 'Yes'}</strong><small> lowest point {money(base.lowest, c)}, {money(base.closing, c)} left by {answer.ends_on ? dateLabel(answer.ends_on) : 'the end'}</small></p>}
    {shortIfRepaid && missing && (
      <p className="journey-warning" role="note">
        <AlertTriangle size={14} /> {money(missing.total, c)} you owe has no due date, so the forecast doesn’t count it
        ({missing.items.slice(0, 3).map(item => item.label).join(', ')}{missing.items.length > 3 ? '…' : ''}).
        Repaying it now would leave you <strong>{money(shortIfRepaid, c)} short</strong>.
      </p>
    )}
    {answer.cautious.verdict === 'short' && base.verdict !== 'short' && (
      <p className="muted-note">If expected income doesn’t arrive: short by {money(answer.cautious.shortfall, c)}.</p>
    )}
  </>
}
