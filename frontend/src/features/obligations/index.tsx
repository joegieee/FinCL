import { WorkspaceReportingDetails, reportedMoney } from '@/lib/workspaceReporting'
import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, RefreshCw } from 'lucide-react'
import { Card } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { AsyncView } from '@/components/AsyncView'
import { money } from '@/lib/api'
import { dateLabel } from '@/lib/format'
import {
  loadObligations,
  type AmountBasis,
  type Obligation,
  type ObligationOrigin,
  type ObligationsPage,
} from './api'

const ORIGIN_LABEL: Record<ObligationOrigin, string> = {
  personal_debt: 'Personal debt',
  installment_plan: 'Installment plan',
  card_statement: 'Card statement',
  recurring_commitment: 'Recurring commitment',
}

const AMOUNT_BASIS_LABEL: Record<AmountBasis, string> = {
  remaining_schedule: 'Remaining schedule',
  lump_sum_due: 'Lump-sum due',
  unknown: 'No evidenced basis',
}

const DATE_BASIS_LABEL: Record<string, string> = {
  stated_due_date: 'Stated due date',
  no_stated_date: 'No stated date',
  scheduled_due_date: 'Scheduled due date',
  no_remaining_due_date: 'No remaining due date',
  statement_due_date: 'Statement due date',
  statement_due_date_not_printed: 'Statement due date not printed',
  scheduled_occurrence: 'Scheduled occurrence',
  no_scheduled_occurrence: 'No scheduled occurrence',
}

const CASH_EFFECT_LABEL: Record<Obligation['cash_effect'], string> = {
  bank_withdrawal: 'Bank withdrawal',
  via_card_statement: 'Settles via card statement',
  unknown: 'Cash effect unknown',
}

/** "Likely income" is named apart from a plain "Confirmed" so a history-derived
 * receivable never reads as a settled commitment. */
function confidenceLabel(o: Obligation): string {
  const income = o.direction === 'inflow'
  if (o.confidence === 'likely') return income ? 'Likely income' : 'Likely'
  return income ? 'Confirmed income' : 'Confirmed'
}

function dateBasisText(kind: string): string {
  return DATE_BASIS_LABEL[kind] ?? kind.replaceAll('_', ' ')
}

/**
 * Cash-flow framework Stage 2B — the unified obligations surface.
 *
 * Read-only. Debts, installment plans, card statement dues and recurring
 * commitments each state an obligation with their own evidence; this merges
 * them behind one shape without flattening what makes them different. Rows are
 * grouped by confidence (a recorded commitment or an evidenced future receipt
 * is Confirmed; anything derived from history alone is Likely and stops there).
 * An obligation whose cash-settlement requirement the evidence cannot resolve
 * is shown as a named gap, never as a zero and never omitted.
 */
export function ObligationsView() {
  const [page, setPage] = useState<ObligationsPage | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const seq = useRef(0)

  const load = useCallback(() => {
    const id = ++seq.current
    setLoading(true); setError('')
    loadObligations()
      .then(p => { if (id === seq.current) setPage(p) })
      .catch(e => { if (id === seq.current) setError((e as Error).message) })
      .finally(() => { if (id === seq.current) setLoading(false) })
  }, [])
  useEffect(() => { load() }, [load])

  return (
    <AsyncView loading={loading} error={error} data={page} onRetry={load} loadingLabel="Loading obligations" errorTitle="We couldn’t load obligations.">
      {page => <ObligationsContent page={page} loading={loading} load={load} />}
    </AsyncView>
  )
}

function ObligationsContent({ page, loading, load }: { page: ObligationsPage; loading: boolean; load: () => void }) {
  const confirmed = page.obligations.filter(o => o.confidence === 'confirmed')
  const likely = page.obligations.filter(o => o.confidence === 'likely')
  const unresolved = page.obligations.filter(o => o.amount_basis === 'unknown')
  const currencies = Object.keys(page.totals_by_currency).sort()

  return (
    <div className="obligations-view"><WorkspaceReportingDetails data={page}/>
      <div className="scope-caption">
        <span>
          {page.obligations.length} obligation{page.obligations.length === 1 ? '' : 's'} as of {page.as_of}
          {' · '}merged from debts, installments, card statements and recurring commitments
        </span>
        <Button variant="ghost" size="sm" onClick={load} disabled={loading}>
          <RefreshCw size={14} className={loading ? 'spin' : ''} /> Refresh
        </Button>
      </div>

      {unresolved.length > 0 && (
        <div className="alert" role="status">
          <div>
            <strong>
              {unresolved.length} obligation{unresolved.length === 1 ? '' : 's'} carr
              {unresolved.length === 1 ? 'ies' : 'y'} an unresolved cash-settlement requirement.
            </strong>
            <p>
              These are listed below as a named gap. They are not a zero and are not counted in the
              totals — the plan is withheld until the evidence resolves the amount.
            </p>
          </div>
        </div>
      )}

      <Card className="obl-card">
        <div className="panel-title">
          <div>
            <p className="eyebrow">Remaining, by cash effect</p>
            <h2>What is still owed</h2>
          </div>
        </div>
        {currencies.length === 0 ? (
          <p className="muted-note">No outflow obligation currently has a countable remaining amount.</p>
        ) : (
          currencies.map(currency => {
            const lanes = page.totals_by_currency[currency]
            return (
              <dl className="baseline-metrics" key={currency}>
                <div><dt>{currency} · bank withdrawal</dt><dd>{reportedMoney(lanes, 'bank_withdrawal', currency)}</dd></div>
                <div><dt>{currency} · via card statement</dt><dd>{reportedMoney(lanes, 'via_card_statement', currency)}</dd></div>
              </dl>
            )
          })
        )}
        <p className="muted-note">
          An obligation settled inside a card statement due is counted in its own lane only, never
          again as a bank withdrawal. A superseded lump sum is shown on its row but never counted.
        </p>
      </Card>

      <ObligationGroup
        title="Confirmed"
        eyebrow="Recorded commitment or evidenced future receipt"
        caption="A recorded commitment states the obligation, or a future receipt is evidenced."
        rows={confirmed}
      />
      <ObligationGroup
        title="Likely"
        eyebrow="Derived from recorded history only"
        caption="History supports “Likely” and stops there. A past receipt does not evidence a future one, so nothing here is automatically confirmed."
        rows={likely}
        muted
      />

      {page.obligations.length === 0 && (
        <Card className="obl-card">
          <div className="empty">
            <AlertTriangle size={24} />
            <h3>No obligations recorded</h3>
            <p>Debts, installment plans, card facilities and recurring commitments appear here once recorded.</p>
          </div>
        </Card>
      )}

      <Card className="obl-card">
        <div className="panel-title"><div><p className="eyebrow">How this merge stays honest</p><h2>Rules</h2></div></div>
        <ul className="issue-list">
          {page.rules.map((rule, i) => <li key={i}><p>{rule}</p></li>)}
        </ul>
      </Card>
    </div>
  )
}

function ObligationGroup({
  title, eyebrow, caption, rows, muted = false,
}: {
  title: string
  eyebrow: string
  caption: string
  rows: Obligation[]
  muted?: boolean
}) {
  if (rows.length === 0) return null
  return (
    <Card className={`obl-card${muted ? ' obl-group-likely' : ''}`}>
      <div className="panel-title">
        <div>
          <p className="eyebrow">{eyebrow}</p>
          <h2>{title} · {rows.length} obligation{rows.length === 1 ? '' : 's'}</h2>
        </div>
        {muted && <Badge variant="outline" className="review-badge">History-derived</Badge>}
      </div>
      <p className="muted-note">{caption}</p>
      <table className="pocket-table">
        <thead>
          <tr>
            <th>Origin adapter</th>
            <th>Obligation</th>
            <th>Remaining amount</th>
            <th>Amount / date basis</th>
            <th>Confidence</th>
            <th>Next expected payment</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(o => <ObligationRow key={o.obligation_id} o={o} />)}
        </tbody>
      </table>
    </Card>
  )
}

function ObligationRow({ o }: { o: Obligation }) {
  const superseded = o.superseded_lump_sum
  const unmatched = o.unmatched_past_occurrences?.length ?? 0
  return (
    <tr>
      <td>
        {ORIGIN_LABEL[o.origin]}
        <small>{CASH_EFFECT_LABEL[o.cash_effect]}</small>
      </td>
      <td>
        <strong>{o.label}</strong>
        <small>{o.currency} · {o.direction === 'inflow' ? 'owed to you' : 'you owe'}</small>
      </td>
      <td>
        {o.remaining_amount ? (
          <>
            {reportedMoney(o.remaining_amount, 'value', o.currency)}
            {superseded && (
              <small>supersedes {reportedMoney(superseded, 'value', superseded.currency)} lump sum — not counted</small>
            )}
          </>
        ) : (
          <>
            <span className="obl-gap">Unresolved requirement</span>
            <small>{o.remaining_unknown_reason ?? 'the amount is not evidenced'}</small>
          </>
        )}
      </td>
      <td>
        {AMOUNT_BASIS_LABEL[o.amount_basis]}
        <small>{dateBasisText(o.date_basis.kind)}</small>
      </td>
      <td>
        <Badge variant="outline" className={o.confidence === 'likely' ? 'review-badge' : ''}>
          {confidenceLabel(o)}
        </Badge>
        <small title={o.confidence_basis}>
          {o.payment_status === 'unknown'
            ? 'no payment recorded yet'
            : o.payment_status.replaceAll('_', ' ')}
        </small>
      </td>
      <td>
        {o.date_basis.value ? dateLabel(o.date_basis.value) : dateBasisText(o.date_basis.kind)}
        {o.overdue && (
          <div><Badge variant="outline" className="review-badge">Overdue — not treated as missed</Badge></div>
        )}
        {unmatched > 0 && (
          <small>{unmatched} unmatched past occurrence{unmatched === 1 ? '' : 's'}</small>
        )}
        {o.actual_payments.count > 0 && (
          <small>{o.actual_payments.count} payment{o.actual_payments.count === 1 ? '' : 's'} recorded · {reportedMoney(o.actual_payments, 'total', o.currency)}</small>
        )}
      </td>
    </tr>
  )
}
