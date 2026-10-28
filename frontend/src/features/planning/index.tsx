import { WorkspaceReportingDetails, reportedMoney } from '@/lib/workspaceReporting'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Archive, ArchiveRestore, Check, ChevronRight, RefreshCw, Target, Wallet } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { money } from '@/lib/api'
import { useLedgerCommand } from '@/lib/command'
import {
  loadBackdatePreview, loadBudgetHistory, loadBudgets, loadGoalScenario, loadGoals,
  loadOccurrences, loadRecurring, monthlyDates, thisMonth,
  type BackdatePreview, type BudgetCard, type BudgetsPage, type GoalCard, type GoalScenario,
  type GoalsPage, type OccurrencesPage, type RecurringPage,
} from './api'

const STATUS: Record<string, string> = {
  settled: 'Settled', observed_so_far: 'So far', coverage_unknown: 'Coverage unknown', archived: 'Archived',
}

/**
 * v0.6 planning surface — dated budgets, savings goals and recurring commitments.
 * Budgets are month-boundary limits (never forecasts); a category budget is gross
 * purchases, an all-spending budget also includes fees; overlapping budgets are
 * independent and never summed. Goal progress is absolute, manually recorded.
 */
export function PlanningView({ generation, freshness, categories, onSaved }: {
  generation: string
  freshness: string
  categories: string[]
  onSaved: (status: string) => void | Promise<void>
}) {
  const [month, setMonth] = useState(thisMonth())
  const disabled = freshness === 'stale'
  return (
    <div className="planning-view">
      {disabled && <div className="alert" role="status">New ledger inputs are available. Refresh before editing plans.</div>}
      <BudgetsSection month={month} setMonth={setMonth} generation={generation} disabled={disabled} categories={categories} onSaved={onSaved} />
      <GoalsSection generation={generation} disabled={disabled} onSaved={onSaved} />
      <RecurringSection generation={generation} disabled={disabled} onSaved={onSaved} />
    </div>
  )
}

function useResource<T>(load: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const seq = useRef(0)
  const reload = useCallback(() => {
    const id = ++seq.current
    setLoading(true); setError('')
    load()
      .then(d => { if (id === seq.current) setData(d) })
      .catch(e => { if (id === seq.current) setError((e as Error).message) })
      .finally(() => { if (id === seq.current) setLoading(false) })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  useEffect(() => { setData(null); reload() }, [reload])
  return { data, error, loading, reload }
}

function BudgetsSection({ month, setMonth, generation, disabled, categories, onSaved }: {
  month: string; setMonth: (m: string) => void; generation: string; disabled: boolean
  categories: string[]; onSaved: (status: string) => void | Promise<void>
}) {
  const { data, error, loading, reload } = useResource<BudgetsPage>(() => loadBudgets(month, 'SGD'), [month])
  const [creating, setCreating] = useState(false)
  const afterSave = async (s: string) => { await onSaved(s); reload() }

  return (
    <Card className="planning-section">
      <div className="panel-title">
        <div><p className="eyebrow">MONTHLY BUDGETS</p><h2>Limits for {month}</h2></div>
        <div className="row-actions">
          <label className="month-pick">Month<Input type="month" value={month} onChange={e => setMonth(e.target.value)} /></label>
          <Button variant="ghost" size="sm" onClick={reload} disabled={loading}><RefreshCw size={14} className={loading ? 'spin' : ''} /></Button>
          <Button size="sm" disabled={disabled} onClick={() => setCreating(v => !v)}>{creating ? 'Cancel' : 'New budget'}</Button>
        </div>
      </div>
      {error && <div className="alert error" role="alert"><p>{error}</p></div>}
      {creating && (
        <BudgetForm generation={generation} month={month} categories={categories} disabled={disabled}
          onSaved={async s => { setCreating(false); await afterSave(s) }} />
      )}
      {data && data.budgets.length === 0 && !creating && (
        <p className="muted-note">No budget is active for {month}. Create one with an explicit first month.</p>
      )}
      {data && <WorkspaceReportingDetails data={data}/>}
      {data?.budgets.map(b => (
        <BudgetRow key={b.plan} budget={b} monthCovered={data.month_covered} generation={generation}
          categories={categories} disabled={disabled} onSaved={afterSave} />
      ))}
      <p className="muted-note">Category budgets count gross purchases; an all-spending budget also includes fees. Overlapping budgets are independent and never summed. A remaining allowance shows only for a complete covered month.</p>
    </Card>
  )
}

function BudgetRow({ budget, generation, categories, disabled, onSaved }: {
  budget: BudgetCard; monthCovered: boolean; generation: string; categories: string[]
  disabled: boolean; onSaved: (status: string) => void | Promise<void>
}) {
  const [mode, setMode] = useState<'idle' | 'revise' | 'archive' | 'history'>('idle')
  const pct = Math.max(0, Math.min(100, (Number(budget.actual) / Number(budget.limit || '1')) * 100))
  return (
    <div className="budget-row">
      <div className="budget-head">
        <div>
          <strong>{budget.name}</strong>
          {budget.archived && <Badge variant="outline"> archived</Badge>}
          <small>{budget.category_key ? budget.category_key : 'All purchases + fees'} · {budget.currency} · {budget.effective_month ? `from ${budget.effective_month}` : 'legacy undated limit'}</small>
        </div>
        <Badge variant="outline" className={budget.status === 'coverage_unknown' ? 'review-badge' : ''}>{STATUS[budget.status]}</Badge>
      </div>
      <div className="budget-values">
        <strong>{reportedMoney(budget, 'actual', budget.currency)}</strong>
        <span>of {reportedMoney(budget, 'limit', budget.currency)}</span>
      </div>
      <div className="progress-track" role="progressbar" aria-label={`${budget.name} spent`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(pct)}>
        <span style={{ width: `${pct}%` }} />
      </div>
      <small>{budget.remaining !== null
        ? `${reportedMoney(budget, 'remaining', budget.currency)} ${Number(budget.remaining) < 0 ? 'over' : 'left'}`
        : 'Observed spending so far — no confident remaining for an incomplete month.'}</small>
      <div className="row-actions">
        <Button variant="ghost" size="sm" disabled={disabled} onClick={() => setMode(m => m === 'revise' ? 'idle' : 'revise')}>Revise</Button>
        <Button variant="ghost" size="sm" disabled={disabled} onClick={() => setMode(m => m === 'archive' ? 'idle' : 'archive')}>
          {budget.archived ? <><ArchiveRestore size={13} /> Restore</> : <><Archive size={13} /> Archive</>}
        </Button>
        <Button variant="ghost" size="sm" onClick={() => setMode(m => m === 'history' ? 'idle' : 'history')}>History <ChevronRight size={13} /></Button>
      </div>
      {mode === 'revise' && (
        <BudgetForm generation={generation} month={budget.effective_month ?? thisMonth()} categories={categories} disabled={disabled}
          plan={budget.plan} initial={budget} onSaved={async s => { setMode('idle'); await onSaved(s) }} />
      )}
      {mode === 'archive' && (
        <ArchiveForm generation={generation} kind="BUDGET_ARCHIVE_SET" anchors={{ plan: budget.plan }}
          extraPayload={{ effective_month: budget.effective_month ?? thisMonth() }} archived={budget.archived} disabled={disabled || budget.legacy_undated}
          onSaved={async s => { setMode('idle'); await onSaved(s) }} />
      )}
      {mode === 'history' && <BudgetHistoryPanel plan={budget.plan} />}
    </div>
  )
}

function BudgetForm({ generation, month, categories, disabled, plan, initial, onSaved }: {
  generation: string; month: string; categories: string[]; disabled: boolean
  plan?: string; initial?: BudgetCard; onSaved: (status: string) => void | Promise<void>
}) {
  const [effectiveMonth, setEffectiveMonth] = useState(month)
  const [name, setName] = useState(initial?.name ?? '')
  const [category, setCategory] = useState(initial?.category_key ?? '')
  const [limit, setLimit] = useState(initial?.limit ?? '')
  const [reason, setReason] = useState('')
  const idRef = useRef(plan ?? crypto.randomUUID())
  const cmd = useLedgerCommand(generation, onSaved)
  return (
    <form className="planning-form" onChange={cmd.reset} onSubmit={e => {
      e.preventDefault()
      void cmd.act({
        kind: 'BUDGET_REVISION_SET',
        anchors: { plan: idRef.current, revision: crypto.randomUUID() },
        payload: {
          effective_month: effectiveMonth, name: name.trim(), currency: initial?.currency ?? 'SGD',
          category_key: category || null, limit: limit.trim(), reason,
        },
      })
    }}>
      <div className="form-row-3">
        <label>From month<Input required type="month" value={effectiveMonth} disabled={cmd.attempted} onChange={e => setEffectiveMonth(e.target.value)} /></label>
        <label>Name<Input required maxLength={80} value={name} disabled={cmd.attempted} onChange={e => setName(e.target.value)} /></label>
        <label>Monthly limit<Input required inputMode="decimal" pattern="[0-9]+(\.[0-9]{1,2})?" value={limit} disabled={cmd.attempted} onChange={e => setLimit(e.target.value)} placeholder="0.00" /></label>
      </div>
      <label>Category<select value={category} disabled={cmd.attempted} onChange={e => setCategory(e.target.value)}>
        <option value="">All purchases + fees</option>
        {categories.filter(c => !c.startsWith('Internal')).map(c => <option key={c}>{c}</option>)}
      </select></label>
      <label>Reason<Input required maxLength={2000} value={reason} disabled={cmd.attempted} onChange={e => setReason(e.target.value)} placeholder="A short note for your decision history" /></label>
      {cmd.error && <p className="form-error" role="alert">{cmd.error}</p>}
      {plan && limit.trim() && effectiveMonth < thisMonth() && (
        <BackdateImpact plan={idRef.current} effectiveMonth={effectiveMonth} limit={limit.trim()}
          currency={initial?.currency ?? 'SGD'} categoryKey={category || null} />
      )}
      {cmd.previewed && <div className="preview" role="status"><Check size={15} /><p>{plan ? 'Revises' : 'Creates'} “{name.trim()}” — {money(limit, initial?.currency ?? 'SGD')}/month from {effectiveMonth}. Applies from that month until the next revision.</p></div>}
      {disabled && <p className="form-error">Refresh the ledger before editing budgets.</p>}
      <div className="dialog-actions">
        <Button type="submit" disabled={cmd.busy || disabled}>{cmd.busy ? 'Working…' : cmd.previewed ? (cmd.attempted ? 'Retry same save' : 'Save revision') : 'Preview'}</Button>
      </div>
    </form>
  )
}

function BackdateImpact({ plan, effectiveMonth, limit, currency, categoryKey }: {
  plan: string; effectiveMonth: string; limit: string; currency: string; categoryKey: string | null
}) {
  const { data, error } = useResource<BackdatePreview>(
    () => loadBackdatePreview(plan, effectiveMonth, limit, currency, categoryKey),
    [plan, effectiveMonth, limit, currency, categoryKey],
  )
  if (error) return <p className="form-error" role="alert">{error}</p>
  if (!data) return <p className="muted-note">Checking which months this backdated revision changes…</p>
  if (data.affected_months.length === 0) return <p className="muted-note">{data.note ?? 'No earlier displayed month changes.'}</p>
  return (
    <div className="history-panel">
      <p className="eyebrow">BACKDATING AFFECTS {data.affected_months.length} MONTH{data.affected_months.length === 1 ? '' : 'S'}</p>
      <table className="occurrence-table">
        <thead><tr><th>Month</th><th>Limit</th><th>Actual</th><th>Remaining</th></tr></thead>
        <tbody>
          {data.affected_months.map(m => (
            <tr key={m.month}>
              <td>{m.month}</td>
              <td>{m.previous_limit ? `${reportedMoney(m, 'previous_limit', currency)} → ` : ''}{reportedMoney(m, 'new_limit', currency)}</td>
              <td>{reportedMoney(m, 'actual', currency)}</td>
              <td>{m.new_remaining !== null
                ? `${m.previous_remaining !== null ? `${reportedMoney(m, 'previous_remaining', currency)} → ` : ''}${reportedMoney(m, 'new_remaining', currency)}`
                : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted-note">A revision never silently rewrites a previously shown historical limit — this is the change you are about to make.</p>
    </div>
  )
}

function BudgetHistoryPanel({ plan }: { plan: string }) {
  const { data, error } = useResource(() => loadBudgetHistory(plan), [plan])
  if (error) return <p className="form-error" role="alert">{error}</p>
  if (!data) return <p className="muted-note">Loading history…</p>
  return (
    <div className="history-panel">
      <p className="eyebrow">DATED REVISIONS</p>
      <ul>
        {data.revisions.map(r => <li key={r.revision}>{r.effective_month}: {reportedMoney(r, 'limit', r.currency)} · {r.category_key ?? 'all + fees'} · “{r.name}”</li>)}
      </ul>
      {data.archive_periods.length > 0 && (
        <>
          <p className="eyebrow">ARCHIVE</p>
          <ul>{data.archive_periods.map((a, i) => <li key={i}>{a.effective_month}: {a.archived ? 'archived' : 'restored'}</li>)}</ul>
        </>
      )}
    </div>
  )
}

function GoalsSection({ generation, disabled, onSaved }: {
  generation: string; disabled: boolean; onSaved: (status: string) => void | Promise<void>
}) {
  const { data, error, loading, reload } = useResource<GoalsPage>(() => loadGoals(), [])
  const [creating, setCreating] = useState(false)
  const afterSave = async (s: string) => { await onSaved(s); reload() }
  return (
    <Card className="planning-section">
      <div className="panel-title">
        <div><p className="eyebrow">SAVINGS GOALS</p><h2>Manually recorded progress</h2></div>
        <div className="row-actions">
          <Button variant="ghost" size="sm" onClick={reload} disabled={loading}><RefreshCw size={14} className={loading ? 'spin' : ''} /></Button>
          <Button size="sm" disabled={disabled} onClick={() => setCreating(v => !v)}>{creating ? 'Cancel' : 'New goal'}</Button>
        </div>
      </div>
      {error && <div className="alert error" role="alert"><p>{error}</p></div>}
      {creating && <GoalForm generation={generation} disabled={disabled} onSaved={async s => { setCreating(false); await afterSave(s) }} />}
      {data && data.goals.length === 0 && !creating && <p className="muted-note">No goals yet. A new goal needs a dated first observation, including an explicit zero.</p>}
      {data && <WorkspaceReportingDetails data={data}/>}
      {data?.goals.map(g => (
        <GoalRow key={g.plan} goal={g} generation={generation} disabled={disabled} onSaved={afterSave} />
      ))}
      <p className="muted-note">Progress is an absolute recorded amount, never a delta. Goals can share money — no “total savings” is derived by summing them.</p>
    </Card>
  )
}

function GoalRow({ goal, generation, disabled, onSaved }: {
  goal: GoalCard; generation: string; disabled: boolean; onSaved: (status: string) => void | Promise<void>
}) {
  const [mode, setMode] = useState<'idle' | 'observe' | 'edit' | 'archive' | 'scenario'>('idle')
  const pct = goal.saved !== null ? Math.max(0, Math.min(100, (Number(goal.saved) / Number(goal.target || '1')) * 100)) : 0
  return (
    <div className="budget-row">
      <div className="budget-head">
        <div>
          <strong><Target size={14} /> {goal.name}</strong>
          {goal.archived && <Badge variant="outline"> archived</Badge>}
          {goal.overdue && <Badge variant="outline" className="review-badge"> overdue</Badge>}
          <small>{goal.currency}{goal.target_on ? ` · target ${goal.target_on}` : ''}{goal.observed_on ? ` · last ${goal.observed_on}` : ''}</small>
        </div>
      </div>
      <div className="budget-values">
        <strong>{goal.saved !== null ? reportedMoney(goal, 'saved', goal.currency) : 'No dated progress'}</strong>
        <span>of {reportedMoney(goal, 'target', goal.currency)}</span>
      </div>
      <div className="progress-track" role="progressbar" aria-label={`${goal.name} progress`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(pct)}>
        <span style={{ width: `${pct}%` }} />
      </div>
      <small>{goal.remaining !== null ? `${reportedMoney(goal, 'remaining', goal.currency)} to go` : 'Record a dated observation to see progress.'}</small>
      <div className="row-actions">
        <Button variant="ghost" size="sm" disabled={disabled} onClick={() => setMode(m => m === 'observe' ? 'idle' : 'observe')}>Add progress</Button>
        <Button variant="ghost" size="sm" disabled={disabled} onClick={() => setMode(m => m === 'edit' ? 'idle' : 'edit')}>Edit target</Button>
        <Button variant="ghost" size="sm" disabled={disabled} onClick={() => setMode(m => m === 'archive' ? 'idle' : 'archive')}>
          {goal.archived ? 'Restore' : 'Archive'}
        </Button>
        {goal.target_on && <Button variant="ghost" size="sm" onClick={() => setMode(m => m === 'scenario' ? 'idle' : 'scenario')}>Plan contributions</Button>}
      </div>
      {mode === 'scenario' && goal.target_on && <ScenarioPanel plan={goal.plan} currency={goal.currency} targetOn={goal.target_on} />}
      {mode === 'observe' && <ObservationForm generation={generation} plan={goal.plan} currency={goal.currency} disabled={disabled} onSaved={async s => { setMode('idle'); await onSaved(s) }} />}
      {mode === 'edit' && <GoalForm generation={generation} disabled={disabled} plan={goal.plan} initial={goal} onSaved={async s => { setMode('idle'); await onSaved(s) }} />}
      {mode === 'archive' && (
        <ArchiveForm generation={generation} kind="GOAL_ARCHIVE_SET" anchors={{ plan: goal.plan }}
          archived={goal.archived} disabled={disabled} onSaved={async s => { setMode('idle'); await onSaved(s) }} />
      )}
    </div>
  )
}

function ScenarioPanel({ plan, currency, targetOn }: { plan: string; currency: string; targetOn: string }) {
  const [day, setDay] = useState(1)
  const [result, setResult] = useState<GoalScenario | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  async function run() {
    setBusy(true); setError(''); setResult(null)
    const from = thisMonth().length === 7 ? `${thisMonth()}-01` : thisMonth()
    const nextMonth = new Date(from + 'T12:00:00'); nextMonth.setMonth(nextMonth.getMonth() + 1)
    const fromMonth = `${nextMonth.getFullYear()}-${String(nextMonth.getMonth() + 1).padStart(2, '0')}`
    try {
      setResult(await loadGoalScenario(plan, monthlyDates(day, fromMonth, targetOn)))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="planning-form">
      <div className="form-row-3">
        <label>Contribution day<Input type="number" min={1} max={31} value={day} onChange={e => setDay(Math.max(1, Math.min(31, Number(e.target.value) || 1)))} /></label>
        <div className="dialog-actions" style={{ marginTop: 22 }}><Button type="button" onClick={run} disabled={busy}>{busy ? 'Working…' : 'Estimate'}</Button></div>
      </div>
      {error && <p className="form-error" role="alert">{error}</p>}
      {result && (
        <div className="preview" role="status">
          <div>
            {result.status === 'scheduled'
              ? <p><strong>{reportedMoney(result, 'monthly_amount', currency)}</strong> per contribution across {result.contribution_dates_considered.length} dates to reach the target by {targetOn}.</p>
              : result.status === 'reached'
                ? <p>Target already reached — {reportedMoney(result, 'remaining', currency)} remaining.</p>
                : <p>{reportedMoney(result, 'remaining', currency)} remaining and no contribution date falls before {targetOn}{result.status === 'overdue' ? ' (overdue)' : ''}.</p>}
            <p className="muted-note">A scenario only — no interest or investment return is assumed, and this reserves no money.</p>
          </div>
        </div>
      )}
    </div>
  )
}

function GoalForm({ generation, disabled, plan, initial, onSaved }: {
  generation: string; disabled: boolean; plan?: string; initial?: GoalCard
  onSaved: (status: string) => void | Promise<void>
}) {
  const [name, setName] = useState(initial?.name ?? '')
  const [target, setTarget] = useState(initial?.target ?? '')
  const [targetOn, setTargetOn] = useState(initial?.target_on ?? '')
  const [reason, setReason] = useState('')
  const idRef = useRef(plan ?? crypto.randomUUID())
  const cmd = useLedgerCommand(generation, onSaved)
  return (
    <form className="planning-form" onChange={cmd.reset} onSubmit={e => {
      e.preventDefault()
      void cmd.act({
        kind: 'GOAL_DETAILS_SET', anchors: { plan: idRef.current },
        payload: {
          name: name.trim(), currency: initial?.currency ?? 'SGD', target: target.trim(),
          target_on: targetOn || null, reason,
        },
      })
    }}>
      <div className="form-row-3">
        <label>Name<Input required maxLength={80} value={name} disabled={cmd.attempted} onChange={e => setName(e.target.value)} /></label>
        <label>Target<Input required inputMode="decimal" pattern="[0-9]+(\.[0-9]{1,2})?" value={target} disabled={cmd.attempted} onChange={e => setTarget(e.target.value)} placeholder="0.00" /></label>
        <label>Target date<Input type="date" value={targetOn} disabled={cmd.attempted} onChange={e => setTargetOn(e.target.value)} /></label>
      </div>
      <label>Reason<Input required maxLength={2000} value={reason} disabled={cmd.attempted} onChange={e => setReason(e.target.value)} /></label>
      {cmd.error && <p className="form-error" role="alert">{cmd.error}</p>}
      {cmd.previewed && <div className="preview" role="status"><Check size={15} /><p>{plan ? 'Updates' : 'Creates'} goal “{name.trim()}”, target {money(target, initial?.currency ?? 'SGD')}. {plan ? '' : 'Record a dated first observation next.'}</p></div>}
      {disabled && <p className="form-error">Refresh the ledger before editing goals.</p>}
      <div className="dialog-actions">
        <Button type="submit" disabled={cmd.busy || disabled}>{cmd.busy ? 'Working…' : cmd.previewed ? (cmd.attempted ? 'Retry same save' : 'Save goal') : 'Preview'}</Button>
      </div>
    </form>
  )
}

function ObservationForm({ generation, plan, currency, disabled, onSaved }: {
  generation: string; plan: string; currency: string; disabled: boolean
  onSaved: (status: string) => void | Promise<void>
}) {
  const [observedOn, setObservedOn] = useState('')
  const [saved, setSaved] = useState('')
  const [reason, setReason] = useState('')
  const cmd = useLedgerCommand(generation, onSaved)
  return (
    <form className="planning-form" onChange={cmd.reset} onSubmit={e => {
      e.preventDefault()
      void cmd.act({
        kind: 'GOAL_OBSERVATION_SET', anchors: { plan, observation: crypto.randomUUID() },
        payload: { observed_on: observedOn, saved: saved.trim(), reason },
      })
    }}>
      <div className="form-row-3">
        <label>Observed on<Input required type="date" value={observedOn} disabled={cmd.attempted} onChange={e => setObservedOn(e.target.value)} /></label>
        <label>Amount saved<Input required inputMode="decimal" pattern="[0-9]+(\.[0-9]{1,2})?" value={saved} disabled={cmd.attempted} onChange={e => setSaved(e.target.value)} placeholder="0.00 — absolute, not a change" /></label>
        <label>Reason<Input required maxLength={2000} value={reason} disabled={cmd.attempted} onChange={e => setReason(e.target.value)} /></label>
      </div>
      {cmd.error && <p className="form-error" role="alert">{cmd.error}</p>}
      {cmd.previewed && <div className="preview" role="status"><Check size={15} /><p>Records {money(saved, currency)} saved as of {observedOn}. This is the absolute amount, not a change. No money moves.</p></div>}
      {disabled && <p className="form-error">Refresh the ledger before recording progress.</p>}
      <div className="dialog-actions">
        <Button type="submit" disabled={cmd.busy || disabled}>{cmd.busy ? 'Working…' : cmd.previewed ? (cmd.attempted ? 'Retry same save' : 'Record progress') : 'Preview'}</Button>
      </div>
    </form>
  )
}

function ArchiveForm({ generation, kind, anchors, extraPayload, archived, disabled, onSaved }: {
  generation: string; kind: string; anchors: Record<string, string>
  extraPayload?: Record<string, unknown>; archived: boolean; disabled: boolean
  onSaved: (status: string) => void | Promise<void>
}) {
  const [reason, setReason] = useState('')
  const cmd = useLedgerCommand(generation, onSaved)
  return (
    <form className="planning-form" onChange={cmd.reset} onSubmit={e => {
      e.preventDefault()
      void cmd.act({ kind, anchors, payload: { ...(extraPayload ?? {}), archived: !archived, reason } })
    }}>
      <label>Reason<Input required maxLength={2000} value={reason} disabled={cmd.attempted} onChange={e => setReason(e.target.value)} placeholder={archived ? 'Why restore it?' : 'Why archive it?'} /></label>
      {cmd.error && <p className="form-error" role="alert">{cmd.error}</p>}
      {cmd.previewed && <div className="preview" role="status"><Check size={15} /><p>{archived ? 'Restores this for new months.' : 'Suppresses this month and later; earlier comparisons stay available.'}</p></div>}
      {disabled && <p className="form-error">Refresh the ledger first.</p>}
      <div className="dialog-actions">
        <Button type="submit" disabled={cmd.busy || disabled}>{cmd.busy ? 'Working…' : cmd.previewed ? (cmd.attempted ? 'Retry same save' : archived ? 'Restore' : 'Archive') : 'Preview'}</Button>
      </div>
    </form>
  )
}

function RecurringSection({ generation, disabled, onSaved }: { generation: string; disabled: boolean; onSaved: (status: string) => void | Promise<void> }) {
  const { data, error, loading, reload } = useResource<RecurringPage>(() => loadRecurring(), [])
  const [open, setOpen] = useState<string | null>(null)
  return (
    <Card className="planning-section">
      <div className="panel-title">
        <div><p className="eyebrow">RECURRING COMMITMENTS</p><h2>Confirmed schedules</h2></div>
        <Button variant="ghost" size="sm" onClick={reload} disabled={loading}><RefreshCw size={14} className={loading ? 'spin' : ''} /></Button>
      </div>
      {error && <div className="alert error" role="alert"><p>{error}</p></div>}
      {data && (data.candidates ?? []).length > 0 && <div className="history-panel">
        <p className="eyebrow">CANDIDATES — REVIEW BEFORE CONFIRMING</p>
        {(data.candidates ?? []).map(candidate => <CandidateReview key={candidate.candidate} candidate={candidate}
          generation={generation} disabled={disabled} onSaved={async s => { await onSaved(s); reload() }} />)}
      </div>}
      {data && data.confirmed.length === 0 && data.paused_or_ended.length === 0 && (
        <p className="muted-note">No confirmed commitments yet. Confirm a detected candidate (requires reviewing schedule, account and amount) with <code>fincl recurring commit</code>.</p>
      )}
      {data && [...data.confirmed, ...data.paused_or_ended].map(c => (
        <div className="budget-row" key={c.commitment}>
          <div className="budget-head">
            <div>
              <strong><RefreshCw size={13} /> {c.name}</strong>
              <Badge variant="outline" className={c.status !== 'active' ? 'review-badge' : ''}> {c.status}{c.ended_on ? ` ${c.ended_on}` : ''}</Badge>
              <small>{c.schedule.kind === 'monthly' ? `day ${c.schedule.day}` : `weekly (weekday ${c.schedule.weekday})`} · {reportedMoney(c, 'expected_amount', c.currency)} · matched within ± {money(c.amount_tolerance, c.currency)}</small>
            </div>
            <Button variant="ghost" size="sm" onClick={() => setOpen(o => o === c.commitment ? null : c.commitment)}>Occurrences <ChevronRight size={13} /></Button>
          </div>
          {open === c.commitment && <OccurrenceTable commitment={c.commitment} currency={c.currency} generation={generation} disabled={disabled} onSaved={onSaved} />}
        </div>
      ))}
      <p className="muted-note">Missing is not “unpaid” and never triggers a payment. Forecasts are labeled estimates and stay separate from observed cash flow.</p>
    </Card>
  )
}

function CandidateReview({ candidate, generation, disabled, onSaved }: {
  candidate: import('./api').RecurringCandidate; generation: string; disabled: boolean
  onSaved: (status: string) => void | Promise<void>
}) {
  const [reason, setReason] = useState('')
  const [action, setAction] = useState<'confirm' | 'dismiss'>('confirm')
  const cmd = useLedgerCommand(generation, onSaved)
  const last = new Date(`${candidate.last_observed_on}T00:00:00`)
  const schedule = candidate.cadence === 'monthly'
    ? { kind: 'monthly', day: last.getDate(), anchor_on: candidate.last_observed_on }
    : { kind: 'weekly', weekday: last.getDay() === 0 ? 6 : last.getDay() - 1, anchor_on: candidate.last_observed_on }
  return <form className="planning-form" onChange={cmd.reset} onSubmit={e => {
    e.preventDefault()
    void cmd.act(action === 'dismiss' ? {
      kind: 'RECURRING_CANDIDATE_DISMISS', anchors: { candidate: candidate.candidate }, payload: { reason },
    } : {
      kind: 'RECURRING_COMMITMENT_SET', anchors: { commitment: crypto.randomUUID() },
      payload: { name: candidate.merchant_key, currency: candidate.currency, merchant_key: candidate.merchant_key,
        account_ids: candidate.account_ids, seed_entry_anchors: candidate.seed_entry_anchors, schedule,
        expected_amount: candidate.expected_amount, amount_tolerance: '0.00', status: 'active', ended_on: null, reason },
    })
  }}>
    <strong>{candidate.merchant_key}</strong>
    <small>{candidate.cadence} · {candidate.count} observed · {reportedMoney(candidate, 'expected_amount', candidate.currency)}</small>
    <label>Decision<select value={action} disabled={cmd.attempted} onChange={e => setAction(e.target.value as 'confirm' | 'dismiss')}>
      <option value="confirm">Confirm reviewed schedule</option><option value="dismiss">Dismiss candidate</option>
    </select></label>
    <label>Reason<Input required maxLength={2000} value={reason} disabled={cmd.attempted} onChange={e => setReason(e.target.value)} /></label>
    {cmd.error && <p className="form-error" role="alert">{cmd.error}</p>}
    <Button type="submit" disabled={disabled || cmd.busy}>{cmd.previewed ? 'Save decision' : 'Preview decision'}</Button>
  </form>
}

function OccurrenceTable({ commitment, currency, generation, disabled, onSaved }: {
  commitment: string; currency: string; generation: string; disabled: boolean; onSaved: (status: string) => void | Promise<void>
}) {
  const now = new Date()
  // A year back, so every date in the baseline window can have its payment linked.
  const start = `${now.getFullYear() - 1}-${String(now.getMonth() + 1).padStart(2, '0')}-01`
  // Through the forecast horizon, so an upcoming occurrence's amount can be set.
  const horizon = new Date(now.getFullYear(), now.getMonth() + 7, 0)
  const end = `${horizon.getFullYear()}-${String(horizon.getMonth() + 1).padStart(2, '0')}-${String(horizon.getDate()).padStart(2, '0')}`
  const { data, error } = useResource<OccurrencesPage>(() => loadOccurrences(commitment, start, end), [commitment])
  if (error) return <p className="form-error" role="alert">{error}</p>
  if (!data) return <p className="muted-note">Loading…</p>
  return (
    <table className="occurrence-table">
      <thead><tr><th>Scheduled</th><th>Expected</th><th>State</th><th>Entry</th><th>Review</th></tr></thead>
      <tbody>
        {data.occurrences.map(o => (
          <tr key={o.scheduled_on}>
            <td>{o.scheduled_on}</td>
            <td>{reportedMoney(o, 'expected_amount', currency)}{o.amount_reason && <small title={o.amount_reason}> · varied</small>}</td>
            <td>{o.state.replaceAll('_', ' ')}</td>
            <td className="mono">{o.entry ?? (o.state === 'explicitly_skipped' ? 'skipped' : '—')}</td>
            <td><OccurrenceDecision commitment={commitment} occurrence={o} generation={generation} disabled={disabled} onSaved={onSaved} /></td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function OccurrenceDecision({ commitment, occurrence, generation, disabled, onSaved }: {
  commitment: string; occurrence: import('./api').Occurrence; generation: string; disabled: boolean
  onSaved: (status: string) => void | Promise<void>
}) {
  const [entry, setEntry] = useState(occurrence.entry ?? '')
  const [amount, setAmount] = useState(occurrence.expected_amount)
  const [reason, setReason] = useState('')
  const [action, setAction] = useState<'match' | 'skip' | 'retire' | 'amount' | 'usual'>(occurrence.state === 'explicitly_skipped' || occurrence.state === 'associated' ? 'retire' : 'match')
  const cmd = useLedgerCommand(generation, onSaved)
  return <form className="inline-decision" onChange={cmd.reset} onSubmit={e => {
    e.preventDefault()
    const anchors = { commitment, scheduled_on: occurrence.scheduled_on }
    void cmd.act(action === 'retire'
      ? { kind: 'RECURRING_OCCURRENCE_RETIRE', anchors, payload: { reason } }
      : action === 'amount'
        ? { kind: 'RECURRING_OCCURRENCE_AMOUNT_SET', anchors, payload: { amount: amount.trim(), reason } }
        : action === 'usual'
          ? { kind: 'RECURRING_OCCURRENCE_AMOUNT_RETIRE', anchors, payload: { reason } }
          : { kind: 'RECURRING_OCCURRENCE_SET', anchors, payload: { entry: action === 'skip' ? null : entry.trim(), reason } })
  }}>
    <select aria-label="Occurrence decision" value={action} disabled={cmd.attempted} onChange={e => setAction(e.target.value as 'match' | 'skip' | 'retire' | 'amount' | 'usual')}>
      <option value="match">Match entry</option><option value="skip">Mark skipped</option><option value="retire">Return to detection</option>
      <option value="amount">Expect a different amount</option>{occurrence.amount_reason && <option value="usual">Expect the usual amount</option>}
    </select>
    {action === 'match' && <Input required aria-label="Entry anchor" value={entry} disabled={cmd.attempted} onChange={e => setEntry(e.target.value)} />}
    {action === 'amount' && <Input required inputMode="decimal" pattern="[0-9]+(\.[0-9]{1,2})?" aria-label="Expected amount" value={amount} disabled={cmd.attempted} onChange={e => setAmount(e.target.value)} />}
    <Input required aria-label="Occurrence reason" value={reason} disabled={cmd.attempted} onChange={e => setReason(e.target.value)} />
    {cmd.error && <small className="form-error">{cmd.error}</small>}
    <Button size="sm" type="submit" disabled={disabled || cmd.busy}>{cmd.previewed ? 'Save' : 'Preview'}</Button>
  </form>
}
