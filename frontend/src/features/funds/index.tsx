import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, Check, RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { AsyncView } from '@/components/AsyncView'
import { money } from '@/lib/api'
import { confirmFund, loadFunds, type FundsPage } from './api'

export function FundsView({ onSaved }: { onSaved: (status: string) => void | Promise<void> }) {
  const [page, setPage] = useState<FundsPage | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState('')
  const load = useCallback(() => { setError(''); loadFunds().then(setPage).catch(e => setError((e as Error).message)) }, [])
  useEffect(() => { load() }, [load])
  async function confirm(id: string) { if (!page) return; setBusy(id); setError(''); try { const r = await confirmFund(id, page.generation, 'Confirmed from the sourced obligation shown here'); await onSaved(r.status); load() } catch (e) { setError((e as Error).message) } finally { setBusy('') } }
  return (
    <AsyncView loading={!page && !error} error={error} data={page} onRetry={load} loadingLabel="Loading annual funds" errorTitle="We couldn’t load annual funds.">
      {page => <FundsContent page={page} load={load} confirm={confirm} busy={busy} />}
    </AsyncView>
  )
}

function FundsContent({ page, load, confirm, busy }: { page: FundsPage; load: () => void; confirm: (id: string) => void; busy: string }) {
  return <div className="funds-view">
    <><div className="scope-caption"><span>Targets, planned contributions, recorded earmarks and cash remain separate.</span><Button variant="ghost" size="sm" onClick={load}><RefreshCw size={14}/> Refresh</Button></div>
      {Object.entries(page.cash_separation.by_currency).map(([currency, c]) => <Card className="controls-card" key={currency}><div className="panel-title"><div><p className="eyebrow">CASH SEPARATION · {currency}</p><h2>{c.available ? `${money(c.unallocated_cash, currency)} unallocated cash` : 'Cash separation unavailable'}</h2></div></div>{c.available ? <dl className="baseline-metrics"><div><dt>Total cash</dt><dd>{money(c.total_cash, currency)}</dd></div><div><dt>Earmarked funds</dt><dd>{money(c.earmarked_funds, currency)}</dd></div><div><dt>Minimum reserve</dt><dd>{money(c.minimum_reserve, currency)}</dd></div><div><dt>Reserve-adjusted unallocated</dt><dd>{money(c.reserve_adjusted_unallocated_cash, currency)}</dd></div></dl> : <p>{c.reason}</p>}</Card>)}
      <Card className="controls-card"><div className="panel-title"><div><p className="eyebrow">SOURCED PROPOSALS</p><h2>Confirm only what the ledger can support</h2></div></div>{page.proposals.length === 0 && <p className="muted-note">No unconfirmed fund proposals.</p>}{page.proposals.map(p => <div className="budget-row" key={p.proposal_id}><div className="budget-head"><div><strong>{p.label}</strong><small>{p.observed_amount ? money(p.observed_amount.value, p.currency) : 'Amount missing'} · {p.observed_date?.value ?? 'Due date missing'} · {p.recurrence.known ? 'scheduled source' : 'recurrence unproven'}</small></div><Badge variant="outline" className={p.complete ? '' : 'review-badge'}>{p.complete ? 'Sourced' : 'Missing evidence'}</Badge></div>{p.incomplete_reasons.length > 0 && <ul className="issue-list">{p.incomplete_reasons.map(r => <li key={r}>{r}</li>)}</ul>}<div className="row-actions"><Button size="sm" disabled={!p.complete || busy === p.proposal_id} onClick={() => void confirm(p.proposal_id)}>{busy === p.proposal_id ? 'Confirming…' : 'Confirm sourced fund'}</Button></div></div>)}</Card>
      <Card className="controls-card"><div className="panel-title"><div><p className="eyebrow">CONFIRMED FUNDS</p><h2>{page.funds.length} annual or irregular fund{page.funds.length === 1 ? '' : 's'}</h2></div></div>{page.funds.map(f => <div className="budget-row" key={f.fund}><div className="budget-head"><div><strong>{f.name}</strong><small>Due {f.target.due_on} · {f.target.amount_basis}</small></div><Badge variant="outline" className={f.shortfall !== '0.00' ? 'review-badge' : ''}>{f.status}</Badge></div><dl className="baseline-metrics"><div><dt>Target</dt><dd>{money(f.target.amount_due, f.currency)}</dd></div><div><dt>Recorded allocation</dt><dd>{money(f.recorded_allocation.allocated_now, f.currency)}</dd></div><div><dt>Shortfall</dt><dd>{money(f.shortfall, f.currency)}</dd></div><div><dt>Planned contribution</dt><dd>{f.planned_contributions.required_contribution ? `${money(f.planned_contributions.required_contribution, f.currency)} × ${f.planned_contributions.remaining_dates.length}` : f.planned_contributions.reason}</dd></div></dl>{f.insufficient_allocation && <div className="alert" role="status"><AlertTriangle size={16}/><div><strong>Funding shortfall {money(f.insufficient_allocation.amount, f.currency)}</strong><p>{f.insufficient_allocation.note}</p></div></div>}</div>)}{page.funds.length > 0 && <p className="muted-note"><Check size={14}/> Confirmation copied the sourced amount and date; no starting cash, salary, or living-cost amount was entered.</p>}</Card>
    </>
  </div>
}
