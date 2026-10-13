import { WorkspaceReportingDetails, reportedMoney } from '@/lib/workspaceReporting'
import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, CheckCircle2, RefreshCw, ShieldAlert, ShieldCheck } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { AsyncView } from '@/components/AsyncView'
import { money } from '@/lib/api'
import { readerLimitations } from '@/lib/limitations'
import { ReservePolicyForm } from './ReservePolicyForm'
import { loadSufficiency, type SufficiencyCase, type SufficiencyCurrency, type SufficiencyDriver, type SufficiencyPage } from './api'

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

/**
 * The plain answer: will there be enough money, and if not, how short.
 *
 * Every figure here is copied from the forecast that produced it, so this view
 * can never disagree with the Forecast tab. What it adds is the answer itself —
 * a verdict, a date, an amount and the obligations behind it — which previously
 * had to be assembled by reading a monthly table.
 *
 * Guardrails this view must keep:
 * - Running out of cash and dipping into the reserve stay separate verdicts with
 *   separate amounts. Merging them would call spending a buffer a failure.
 * - The headline is the base case, with the conservative case stated beside it.
 *   "Enough unless spending runs high" is its own finding and must stay visible.
 * - A currency whose forecast could not be built shows its reasons and no
 *   verdict. It must never render as a yes.
 */
export function SufficiencyView({
  generation, onSaved, onOpenObligations, onOpenCash,
}: {
  generation: string; onSaved?: (status: string) => void | Promise<void>; onOpenObligations?: () => void
  /** Where a missing answer is fixed: the Cash page names, per pocket, the
   * account and date holding the shared cutoff back. */
  onOpenCash?: () => void
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
      {page => (
        <div className="sufficiency-view"><WorkspaceReportingDetails data={page}/>
          <div className="scope-caption">
            <span>Projected from the supported cash cutoff, on the same evidence as the Forecast tab.</span>
            <Button variant="ghost" size="sm" onClick={load}><RefreshCw size={14} /> Refresh</Button>
          </div>

          {Object.values(page.by_currency).length === 0 && (
            <Card className="controls-card">
              <div className="panel-title"><div><p className="eyebrow">NO ANSWER YET</p><h2>No currency has a usable forecast</h2></div></div>
              <ul className="issue-list">
                {readerLimitations(page.limitations).map((reason, i) => <li key={i}><p>{reason}</p></li>)}
              </ul>
              {onOpenCash && <Button variant="outline" size="sm" onClick={onOpenCash}>See which account is holding this back</Button>}
            </Card>
          )}

          {Object.values(page.by_currency).map(cell => (
            <CurrencyAnswer key={cell.currency} cell={cell} onOpenObligations={onOpenObligations} onOpenCash={onOpenCash} />
          ))}

          <Card className="controls-card">
            <div className="panel-title">
              <div>
                <p className="eyebrow">RESERVE POLICY</p>
                <h2>{page.reserve_policy.months} months of essential spending held back</h2>
              </div>
              <Button variant="outline" size="sm" onClick={() => setEditingPolicy(true)}>
                {page.reserve_policy.is_default ? 'Choose a policy' : 'Change policy'}
              </Button>
            </div>
            <p className="muted-note">
              {page.reserve_policy.is_default
                ? page.reserve_policy.reason
                : `Chosen on ${page.reserve_policy.decided_at?.slice(0, 10)}.`}
              {' '}{page.reserve_policy.basis}.
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
      )}
    </AsyncView>
  )
}

function DriverList({ drivers, currency }: { drivers: SufficiencyDriver[]; currency: string }) {
  return (
    <ul className="driver-list">
      {drivers.map(driver => (
        <li key={driver.origin}>
          <div className="driver-head">
            <strong>{driver.label}</strong>
            <span>{reportedMoney(driver, 'amount', currency)}</span>
          </div>
          {driver.items.length > 0 && (
            <p className="muted-note">
              {driver.items.map(item => `${item.label} (${item.on})`).join(' · ')}
              {driver.item_count > driver.items.length && ` · and ${driver.item_count - driver.items.length} more`}
            </p>
          )}
        </li>
      ))}
    </ul>
  )
}

function CurrencyAnswer({ cell, onOpenObligations, onOpenCash }: { cell: SufficiencyCurrency; onOpenObligations?: () => void; onOpenCash?: () => void }) {
  const base = cell.base
  return (
    <Card className={`sufficiency-card ${TONE[base.verdict]}`}>
      <div className="panel-title">
        <div>
          <p className="eyebrow">WILL I HAVE ENOUGH? · {cell.currency}</p>
          <h2 className="sufficiency-answer"><VerdictIcon verdict={base.verdict} /> {cell.answer}</h2>
        </div>
        {base.shortfall && (
          <Badge variant="outline" className="review-badge">
            short {reportedMoney(base, 'shortfall', cell.currency)}
          </Badge>
        )}
      </div>

      <p className="sufficiency-headline">
        {base.headline}
        {base.shortfall && base.shortfall_on && (
          <> The worst point is {reportedMoney(base, 'shortfall', cell.currency)} on {base.shortfall_on}.</>
        )}
      </p>

      {base.verdict === 'unavailable' && base.reasons.length > 0 && (
        <ul className="issue-list">
          {base.reasons.map((reason, i) => <li key={i}><p>{reason}</p></li>)}
        </ul>
      )}
      {base.verdict === 'unavailable' && onOpenCash && (
        <Button variant="outline" size="sm" onClick={onOpenCash}>See which account is holding this back</Button>
      )}

      {base.drivers.length > 0 && (
        <>
          <p className="eyebrow">WHAT DRIVES IT</p>
          <DriverList drivers={base.drivers} currency={cell.currency} />
        </>
      )}

      {base.verdict !== 'unavailable' && (base.accounted_for?.length ?? 0) > 0 && (
        <>
          <p className="eyebrow">EVERYTHING THIS ANSWER COUNTS</p>
          <DriverList drivers={base.accounted_for} currency={cell.currency} />
          {onOpenObligations && (
            <Button variant="link" size="sm" onClick={onOpenObligations}>See everything owed</Button>
          )}
        </>
      )}

      <div className="sufficiency-cases">
        <p className="muted-note">
          <strong>If spending runs at its observed high:</strong> {cell.conservative.headline}
          {cell.conservative.shortfall && ` Short ${reportedMoney(cell.conservative, 'shortfall', cell.currency)}.`}
        </p>
        {cell.only_safe_in_the_base_case && (
          <p className="muted-note">
            This position holds only while spending stays near its average. It does not
            survive a month like the dearest one already observed.
          </p>
        )}
        <p className="muted-note">
          Opening {reportedMoney(cell, 'starting_balance', cell.currency)} as of {cell.starting_balance_as_of}
          {cell.horizon_end && `, projected through ${cell.horizon_end}`}. Estimate only.
        </p>
      </div>
    </Card>
  )
}
