import { useCallback, useEffect, useState } from 'react'
import { UploadCloud } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { dateLabel } from '@/lib/format'
import { BalanceObservationForm } from '@/features/cash/BalanceObservationForm'
import { CertifyCoverageForm, type CertifyTarget } from '@/features/cash/CertifyCoverageForm'
import { CoveragePanel } from '@/features/cash/CoveragePanel'
import {
  type AccountFreshness, type CashPocket, type CoverageReport, type MovementCertification,
  loadCashPosition, loadCertifications, loadCoverageReport, retireCoverage,
} from '@/features/cash/api'
import { type BaselineReadiness, type Interval, loadBaselineReadiness } from '@/features/baseline/api'
import { type AccountRow, loadAccounts } from '@/features/accounts/api'
import { CloseAccountForm, type ClosingAccount } from './CloseAccountForm'

/** Where the answer's import to-dos land: `?view=Setup#setup-coverage`. */
export const SETUP_COVERAGE_ID = 'setup-coverage'
export const SETUP_IMPORT_ID = 'setup-import'

const BASIS_LABEL: Record<string, string> = {
  statement: 'a statement',
  observation: 'a balance you recorded',
  certification: 'a range you certified as quiet',
}

function monthLabel(month: string) {
  return new Date(`${month}-15T12:00:00`).toLocaleDateString('en-SG', { month: 'short', year: 'numeric' })
}

function monthsLabel(interval: Interval) {
  const first = interval.start.slice(0, 7)
  const last = interval.end.slice(0, 7)
  return first === last ? monthLabel(first) : `${monthLabel(first)} – ${monthLabel(last)}`
}

export function scrollToImport() {
  const target = document.getElementById(SETUP_IMPORT_ID)
  target?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
  target?.querySelector<HTMLInputElement>('input[type="file"]')?.focus()
}

type Row = { account: string; name: string; freshness: AccountFreshness | null; missing: Interval[]; stopped: boolean }

function rows(report: CoverageReport, readiness: BaselineReadiness | null): Row[] {
  const byAccount = new Map<string, Row>()
  for (const freshness of report.freshness) {
    byAccount.set(freshness.account, { account: freshness.account, name: freshness.display_name, freshness, missing: [], stopped: false })
  }
  for (const pocket of readiness?.pockets ?? []) {
    if (pocket.missing_intervals.length === 0) continue
    const row = byAccount.get(pocket.account_id)
      ?? { account: pocket.account_id, name: pocket.display_name ?? pocket.account_id, freshness: null, missing: [], stopped: false }
    if (pocket.stopped) row.stopped = true
    for (const interval of pocket.missing_intervals) {
      if (!row.missing.some(m => m.start === interval.start && m.end === interval.end)) row.missing.push(interval)
    }
    byAccount.set(pocket.account_id, row)
  }
  return [...byAccount.values()]
}

/** The account as the close form needs it; null when the Accounts read has no such account. */
function closable(row: Row, accounts: AccountRow[]): ClosingAccount | null {
  const account = accounts.find(a => a.id === row.account)
  if (!account) return null
  const last = account.pockets.map(p => p.observed_end ?? '').sort().pop() || row.freshness?.evidence_through || ''
  return { id: account.id, displayName: account.display_name, lastActivity: last }
}

/**
 * What each account's statements cover, and every way to close what they
 * don't: upload the missing statement, record a balance read off the account,
 * or certify a range that had no activity. This is where the answer's import
 * to-dos open, so each one lands on a page that can fix it.
 */
export function SetupCoverage({ generation, reloadKey, onSaved }: {
  generation: string
  reloadKey: number
  onSaved: () => void
}) {
  const [report, setReport] = useState<CoverageReport | null>(null)
  const [readiness, setReadiness] = useState<BaselineReadiness | null>(null)
  const [certifications, setCertifications] = useState<MovementCertification[]>([])
  const [pockets, setPockets] = useState<CashPocket[]>([])
  const [observing, setObserving] = useState<CashPocket | null>(null)
  const [certifying, setCertifying] = useState<CertifyTarget | null>(null)
  const [accounts, setAccounts] = useState<AccountRow[]>([])
  const [closing, setClosing] = useState<ClosingAccount | null>(null)

  // Each read degrades on its own: a failure hides that part, never the page.
  const load = useCallback(() => {
    loadCoverageReport().then(setReport).catch(() => setReport(null))
    loadBaselineReadiness().then(setReadiness).catch(() => setReadiness(null))
    loadCertifications().then(r => setCertifications(r.certifications)).catch(() => setCertifications([]))
    loadCashPosition().then(p => setPockets(p.pockets)).catch(() => setPockets([]))
    loadAccounts().then(page => setAccounts(page.accounts)).catch(() => setAccounts([]))
  }, [])
  useEffect(() => { load() }, [load, reloadKey])
  // A to-do that links here with the anchor lands on this card once it exists.
  const shown = report !== null
  useEffect(() => {
    if (shown && window.location.hash === `#${SETUP_COVERAGE_ID}`) {
      document.getElementById(SETUP_COVERAGE_ID)?.scrollIntoView?.({ block: 'start' })
    }
  }, [shown])

  if (!report) return null
  const tableRows = rows(report, readiness)
  if (tableRows.length === 0 && report.remedies.length === 0) return null
  const saved = () => { load(); onSaved() }

  return (
    <>
      <Card className="setup-coverage" id={SETUP_COVERAGE_ID}>
        <div className="panel-title">
          <div>
            <p className="eyebrow">What your statements cover</p>
            <h2>
              {report.supported_cutoff
                ? `Your forecast starts from ${dateLabel(report.supported_cutoff)}`
                : 'Your accounts don’t yet share a starting date'}
            </h2>
          </div>
          <Button variant="outline" size="sm" onClick={scrollToImport}><UploadCloud size={14} /> Go to upload</Button>
        </div>
        <p className="muted-note">
          The forecast starts on the latest day every account can prove. An account behind the others holds that day back
          for all of them.
        </p>
        <div className="table-scroll">
          <table className="coverage-table">
            <thead><tr><th>Account</th><th>Proven through</th><th>Proven by</th><th>Missing months</th></tr></thead>
            <tbody>
              {tableRows.map(row => (
                <tr key={row.account}>
                  <td>
                    {row.name}
                    {row.freshness && <small>{row.freshness.currencies.join(' · ')}</small>}
                    {row.freshness?.holds_back_start && <Badge variant="outline" className="review-badge">Holding the start back</Badge>}
                    {row.freshness?.closed_with.map(held => (
                      <p key={held.currency_pocket} className="muted-note">
                        Marked closed on {dateLabel(row.freshness!.archived_on!)} with {held.currency_pocket} {held.balance} still recorded,
                        so it still counts. Record its balance to settle that.{' '}
                        <Button variant="outline" size="sm" onClick={() => {
                          const match = pockets.find(p => p.account_id === row.account && p.currency_pocket === held.currency_pocket)
                          if (match) setObserving(match)
                        }}>Record balance</Button>
                      </p>
                    ))}
                  </td>
                  <td>
                    {row.freshness?.evidence_through
                      ? <>{dateLabel(row.freshness.evidence_through)}<small className={row.freshness.stale ? 'form-error' : ''}>{row.freshness.age_days} day{row.freshness.age_days === 1 ? '' : 's'} ago</small></>
                      : <span className="form-error">Not proven</span>}
                  </td>
                  <td>{row.freshness?.basis ? BASIS_LABEL[row.freshness.basis] ?? row.freshness.basis : '—'}</td>
                  <td>
                    {row.missing.length ? row.missing.map(monthsLabel).join(', ') : 'None'}
                    {row.stopped && closable(row, accounts) && (
                      <Button variant="outline" size="sm" onClick={() => setClosing(closable(row, accounts))}>This account is closed</Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <CoveragePanel
        summary={report.coverage} partial={report.partial_position} report={report}
        certifications={certifications}
        onCertify={setCertifying}
        onObserve={(accountId, currency) => {
          const match = pockets.find(p => p.account_id === accountId && p.currency_pocket === currency)
          if (match) setObserving(match)
        }}
        onImport={scrollToImport}
        onRetireCertification={async certification => {
          try {
            await retireCoverage(generation, certification.certification, 'no longer certain this range was quiet')
            saved()
          } catch { load() }
        }}
      />

      {observing && (
        <BalanceObservationForm
          pocket={observing} generation={generation}
          onCancel={() => setObserving(null)}
          onSaved={() => { setObserving(null); saved() }}
        />
      )}
      {closing && (
        <CloseAccountForm
          account={closing} generation={generation}
          onCancel={() => setClosing(null)}
          onSaved={() => { setClosing(null); saved() }}
        />
      )}
      {certifying && (
        <CertifyCoverageForm
          target={certifying} generation={generation}
          onCancel={() => setCertifying(null)}
          onSaved={() => { setCertifying(null); saved() }}
        />
      )}
    </>
  )
}
