import { WorkspaceReportingDetails } from '@/lib/workspaceReporting'
import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, ArrowRight, CheckCircle2, Plus, RefreshCw, ShieldAlert, ShieldCheck } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { AsyncView } from '@/components/AsyncView'
import { money } from '@/lib/api'
import { readerLimitations } from '@/lib/limitations'
import { ReservePolicyForm } from './ReservePolicyForm'
import { loadSufficiency, type CombinedAnswer, type SufficiencyCase, type SufficiencyDestination, type SufficiencyPage } from './api'

const TONE: Record<SufficiencyCase['verdict'], string> = {
  enough: 'is-enough',
  below_reserve: 'is-reserve',
  short: 'is-short',
  unavailable: 'is-unknown',
}

function VerdictIcon({ verdict }: { verdict: SufficiencyCase['verdict'] }) {
  if (verdict === 'enough') return <CheckCircle2 size={22} />
  if (verdict === 'below_reserve') return <ShieldAlert size={22} />
  if (verdict === 'short') return <AlertTriangle size={22} />
  return <ShieldCheck size={22} />
}

const DAY = 86_400_000
function dayOf(iso: string) { return Date.UTC(+iso.slice(0, 4), +iso.slice(5, 7) - 1, +iso.slice(8, 10)) }

/** "16 Sep 2026 (in 3 days)" — the date itself, then how far it is from today. */
export function when(iso: string | null | undefined, today: string): string {
  if (!iso) return 'an unknown date'
  const label = new Date(dayOf(iso)).toLocaleDateString('en-SG', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' })
  const days = Math.round((dayOf(iso) - dayOf(today)) / DAY)
  if (days === 0) return `${label} (today)`
  const size = Math.abs(days)
  const span = size < 14 ? `${size} day${size === 1 ? '' : 's'}` : size < 60 ? `${Math.round(size / 7)} weeks` : `${Math.round(size / 30)} months`
  return `${label} (${days > 0 ? `in ${span}` : `${span} ago`})`
}

/** ISO dates inside a sentence, written the way the rest of the page writes them. */
function plainDates(text: string): string {
  return text.replace(/\b(\d{4})-(\d{2})-(\d{2})\b/g, iso =>
    new Date(dayOf(iso)).toLocaleDateString('en-SG', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' }))
}

/**
 * Will I have enough money? One answer, across every pocket.
 *
 * The money in each currency is one pool for one purpose, so the page asks the
 * question once, in the reporting currency, using the same calendar-day sum
 * the Forecast tab uses. There are no per-currency verdicts and no transfer
 * suggestions.
 *
 * Guardrails this view must keep:
 * - Running out of cash and dipping into the reserve stay separate answers.
 * - The cautious case is stated beside the main one, never merged into it.
 * - A pocket that could not be counted is named beside the answer; a missing
 *   answer never renders as a yes.
 * - Every to-do points at the page that fixes it.
 */
export function SufficiencyView({
  generation, onSaved, onOpen, onOpenObligations, onOpenCash, onOpenPlanned,
}: {
  generation: string; onSaved?: (status: string) => void | Promise<void>
  /** Where each "make this answer better" item is fixed. */
  onOpen?: (destination: SufficiencyDestination) => void
  onOpenObligations?: () => void
  /** Where a missing answer is fixed: the Cash page names, per pocket, the
   * account and date holding the shared cutoff back. */
  onOpenCash?: () => void
  /** Where known future money (a bill, a purchase, a trip, income) is added. */
  onOpenPlanned?: () => void
}) {
  const [page, setPage] = useState<SufficiencyPage | null>(null)
  const [error, setError] = useState('')
  const [editingPolicy, setEditingPolicy] = useState(false)

  const load = useCallback(() => {
    setError('')
    loadSufficiency().then(setPage).catch(e => setError((e as Error).message))
  }, [])
  useEffect(() => { load() }, [load])

  return (
    <AsyncView
      loading={!page && !error} error={error} data={page} onRetry={load}
      loadingLabel="Working out whether there is enough"
      errorTitle="We couldn’t work out whether there is enough."
    >
      {page => {
        const combined = page.combined_answer
        const counted = combined && combined.pockets.some(p => p.counted === 'projected' || p.counted === 'held_as_is')
        return (
          <div className="sufficiency-view"><WorkspaceReportingDetails data={page}/>
            <div className="scope-caption">
              <span>An estimate from your imported statements, on the same evidence as the Forecast tab.</span>
              <span>
                {onOpenPlanned && <Button variant="outline" size="sm" onClick={onOpenPlanned}><Plus size={14} /> Add something you know is coming</Button>}
                <Button variant="ghost" size="sm" onClick={load}><RefreshCw size={14} /> Refresh</Button>
              </span>
            </div>

            {!counted && (
              <Card className="controls-card">
                <div className="panel-title"><div><p className="eyebrow">No answer yet</p><h2>There isn’t enough to go on yet</h2></div></div>
                <ul className="issue-list">
                  {readerLimitations(page.limitations).map((reason, i) => <li key={i}><p>{reason}</p></li>)}
                </ul>
                {onOpenCash && <Button variant="outline" size="sm" onClick={onOpenCash}>See which account is holding this back</Button>}
              </Card>
            )}

            {counted && <Answer combined={combined} today={page.generated_for} />}
            {counted && combined.todos.length > 0 && <Todos combined={combined} onOpen={onOpen} />}
            {counted && <Assumptions combined={combined} today={page.generated_for} onOpenObligations={onOpenObligations} />}

            <Card className="controls-card">
              <div className="panel-title">
                <div>
                  <p className="eyebrow">Safety reserve</p>
                  <h2>Keep {page.reserve_policy.months} months of essential spending untouched</h2>
                </div>
                <Button variant="outline" size="sm" onClick={() => setEditingPolicy(true)}>
                  {page.reserve_policy.is_default ? 'Choose a policy' : 'Change policy'}
                </Button>
              </div>
              <p className="muted-note">
                {page.reserve_policy.is_default
                  ? `You haven’t chosen this yet, so ${page.reserve_policy.months} months is used.`
                  : `You chose this on ${when(page.reserve_policy.decided_at?.slice(0, 10), page.generated_for)}.`}
                {' '}You choose the number of months; the monthly amount always comes from your own essential spending.
              </p>
            </Card>

            {editingPolicy && (
              <ReservePolicyForm
                months={page.reserve_policy.months}
                generation={generation}
                onCancel={() => setEditingPolicy(false)}
                onSaved={async status => {
                  setEditingPolicy(false)
                  load()
                  await onSaved?.(status)
                }}
              />
            )}
          </div>
        )
      }}
    </AsyncView>
  )
}

function Answer({ combined, today }: { combined: CombinedAnswer; today: string }) {
  const c = combined.reporting_currency
  const base = combined.base
  const cautious = combined.cautious
  const missing = combined.pockets.filter(p => p.counted === 'not_counted')
  const undated = combined.not_in_the_path
  const shortIfRepaid = undated?.short_if_repaid_now ?? null
  return (
    <Card className={`sufficiency-card ${TONE[base.verdict]}`}>
      <div className="panel-title">
        <div>
          <h2 className="sufficiency-answer"><VerdictIcon verdict={shortIfRepaid && base.verdict === 'enough' ? 'below_reserve' : base.verdict} /> {shortIfRepaid && base.verdict === 'enough' ? 'Yes, but only if your debts can wait' : combined.answer}</h2>
        </div>
      </div>

      <p className="sufficiency-headline">
        {base.verdict === 'enough' && <>Your money lasts until {when(combined.ends_on, today)}, all accounts and currencies together.</>}
        {base.verdict === 'short' && <>You run out around {when(base.first_short_on, today)}. At the worst point, around {when(base.shortfall_on, today)}, you’d be {money(base.shortfall, c)} short.</>}
        {base.verdict === 'below_reserve' && <>You don’t run out, but you dip into your safety reserve around {when(base.first_short_on, today)}, by up to {money(base.shortfall, c)}.</>}
      </p>

      {shortIfRepaid && undated && (
        <div className="sufficiency-undated" role="note">
          <p><strong>{money(undated.total, c)} you owe has no due date</strong>, so no day of this forecast pays it. Repaying it all now would leave you <strong>{money(shortIfRepaid, c)} short</strong> at the lowest point.</p>
          <ul>{undated.items.map(item => <li key={item.label}><span>{item.label}</span><span>{money(item.amount, c)}</span></li>)}</ul>
        </div>
      )}

      <dl className="sufficiency-figures">
        <div><dt>Starting with</dt><dd>{money(combined.starting_total, c)}<small>{when(combined.starts_on, today)}</small></dd></div>
        <div><dt>Lowest point</dt><dd>{money(base.lowest, c)}<small>{when(base.lowest_on, today)}</small></dd></div>
        <div><dt>Left at the end</dt><dd>{money(base.closing, c)}<small>{when(combined.ends_on, today)}</small></dd></div>
      </dl>

      {missing.length > 0 && (
        <p className="sufficiency-missing" role="note">
          Not counted: {missing.map(p => `${p.starting_balance == null ? p.currency : money(p.starting_balance, p.currency)} (${p.why})`).join('; ')}.
        </p>
      )}

      {combined.incomplete_because.length > 0 && (
        <div className="sufficiency-missing" role="note">
          <p>This answer is incomplete because:</p>
          <ul>{combined.incomplete_because.map(reason => <li key={reason}>{reason}</li>)}</ul>
        </div>
      )}

      <div className="sufficiency-cases">
        <p className="muted-note">
          <strong>Cautious case</strong> (likely income doesn’t arrive and spending runs at its highest month):{' '}
          {cautious.verdict === 'enough' && 'you still have enough.'}
          {cautious.verdict === 'short' && <>you’d run out around {when(cautious.first_short_on, today)}, up to {money(cautious.shortfall, c)} short.</>}
          {cautious.verdict === 'below_reserve' && <>you’d dip into your reserve by up to {money(cautious.shortfall, c)}.</>}
          {cautious.verdict === 'unavailable' && 'not enough evidence to say.'}
        </p>
        {combined.only_safe_in_the_base_case && (
          <p className="muted-note">So the answer depends on that income arriving and spending staying near its usual level.</p>
        )}
      </div>
    </Card>
  )
}

function Todos({ combined, onOpen }: { combined: CombinedAnswer; onOpen?: (destination: SufficiencyDestination) => void }) {
  return (
    <Card className="controls-card">
      <div className="panel-title"><div><p className="eyebrow">Make this answer better</p><h2>{combined.todos.length} thing{combined.todos.length === 1 ? '' : 's'} to fix, most useful first</h2></div></div>
      <ol className="todo-list">
        {combined.todos.map(todo => (
          <li key={todo.key}>
            <div>
              <strong>{todo.title}</strong>
              <p className="muted-note">{plainDates(todo.detail)}</p>
            </div>
            {onOpen && (
              <Button variant="outline" size="sm" onClick={() => { if (todo.anchor) window.location.hash = todo.anchor; onOpen(todo.page) }} aria-label={`${todo.title}: open ${todo.page}`}>
                Open {todo.page} <ArrowRight size={14} />
              </Button>
            )}
          </li>
        ))}
      </ol>
    </Card>
  )
}

function Assumptions({ combined, today, onOpenObligations }: { combined: CombinedAnswer; today: string; onOpenObligations?: () => void }) {
  const c = combined.reporting_currency
  return (
    <Card className="controls-card">
      <div className="panel-title"><div><p className="eyebrow">What this assumes</p><h2>What the answer is built on</h2></div></div>

      <h3 className="assumption-heading">Starting money</h3>
      <p className="muted-note">
        {money(combined.starting_total, c)} across your accounts on {when(combined.starts_on, today)}, the last day your statements cover.
      </p>
      <ul className="assumption-list">
        {combined.pockets.map(p => (
          <li key={p.currency}>
            <span>{p.starting_balance == null ? `${p.currency} —` : money(p.starting_balance, p.currency)}</span>
            <span>
              {p.counted === 'ignored' ? 'ignored, too small to matter'
                : p.counted === 'not_counted' ? 'not counted'
                : p.rate_display ? `${money(p.starting_balance_reported, c)} at 1 ${p.currency} = ${p.rate_display} ${c}`
                : 'counted as it is'}
              {p.counted === 'held_as_is' && ' · kept as it is, nothing projected'}
              {p.counted === 'projected' && p.why && ` · ${p.why}`}
            </span>
            {p.accounts.length > 0 && (
              <ul className="assumption-sublist">
                {p.accounts.map(a => (
                  <li key={a.account}>
                    {a.display_name}: {a.evidence_through ? `proven through ${when(a.evidence_through, today)}` : 'nothing proves a balance yet'}
                    {a.stale && ' · out of date'}
                    {a.holds_back_start && ' · holding the start back'}
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>

      <h3 className="assumption-heading">Money coming in</h3>
      {combined.income.length === 0
        ? <p className="muted-note">No income is counted.</p>
        : (
          <ul className="assumption-list">
            {combined.income.map(row => (
              <li key={`${row.currency}:${row.label}`}>
                <span>{row.label} · {row.count} payment{row.count === 1 ? '' : 's'}, next {when(row.next_on, today)}</span>
                <span>
                  {money(row.total, c)}{row.currency !== c && ` (${money(row.native_total, row.currency)})`}
                  {' · '}{row.in_cautious_case ? 'counted in both cases' : 'likely, so left out of the cautious case'}
                </span>
              </li>
            ))}
          </ul>
        )}

      {combined.upside.length > 0 && (
        <>
          <p className="muted-note">Not counted, in case it doesn’t come:</p>
          <ul className="assumption-list">
            {combined.upside.map(row => (
              <li key={`${row.currency}:${row.label}:${row.on}`}>
                <span>{row.label}{row.on && ` · ${when(row.on, today)}`}</span>
                <span>{money(row.amount, row.currency)}</span>
              </li>
            ))}
          </ul>
        </>
      )}

      <h3 className="assumption-heading">Money going out · about {money(combined.monthly_out, c)} a month</h3>
      <ul className="driver-list">
        {combined.where_it_goes.map(row => (
          <li key={row.origin}>
            <div className="driver-head">
              <strong>{row.label}</strong>
              <span>{money(row.total, c)}</span>
            </div>
            {row.origin === 'baseline_spending' && <p className="muted-note">Based on what you usually spend, about {money(row.monthly, c)} a month.</p>}
            {row.items.length > 0 && (
              <p className="muted-note">{row.items.map(item => `${item.label} · ${when(item.on, today)}`).join(' · ')}</p>
            )}
          </li>
        ))}
      </ul>
      {onOpenObligations && <Button variant="link" size="sm" onClick={onOpenObligations}>See everything owed</Button>}

      <h3 className="assumption-heading">Safety reserve</h3>
      <p className="muted-note">
        {combined.reserve.available
          ? `${money(combined.reserve.target, c)} is kept aside; dipping below it is reported separately from running out.`
          : 'Not checked yet, because essential spending hasn’t been marked.'}
      </p>
    </Card>
  )
}
