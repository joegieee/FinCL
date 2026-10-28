import { CalendarCheck, PenLine, ShieldCheck, UploadCloud, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { money } from '@/lib/api'
import type { CoverageRemedy, CoverageReport, MovementCertification } from './api'
import type { CashPageOut } from './reportedApi'
import type { CertifyTarget } from './CertifyCoverageForm'

const UNLOCKS_LABEL: Record<CoverageRemedy['unlocks'], string> = {
  'unblocks-shared-cutoff': 'Restores the complete total',
  'adds-a-pocket': 'Adds this account to the total',
  'brings-forward': 'Brings the total up to today',
}

const ACTION_ICON = {
  'record-balance-observation': PenLine,
  'certify-quiet-interval': CalendarCheck,
  'import-statement': UploadCloud,
} as const

const ACTION_LABEL = {
  'record-balance-observation': 'Record balance',
  'certify-quiet-interval': 'Certify range',
  'import-statement': 'Import a statement',
} as const

/**
 * How much of the cash picture the evidence can currently see, and what would
 * close the rest.
 *
 * The page above this panel keeps the strict answer: a complete total, dated to
 * the last day every pocket can prove. This panel adds the two things that
 * answer alone could not — the most current figure available even when it is
 * partial, and a concrete next step per gap. Neither invents money: the partial
 * total sums only proven pockets and names the ones it left out, and every
 * remedy leads to imported evidence, an observed balance, or an attestation
 * that carries no amount.
 */
export function CoveragePanel({
  summary, partial, report, certifications, onCertify, onObserve, onImport, onRetireCertification,
}: {
  summary: CashPageOut['coverage_summary']
  partial: CashPageOut['partial_position']
  report: CoverageReport | null
  certifications: MovementCertification[]
  onCertify: (target: CertifyTarget) => void
  onObserve: (accountId: string, currencyPocket: string) => void
  onImport: () => void
  onRetireCertification: (certification: MovementCertification) => void
}) {
  const percent = Math.round(summary.ratio * 100)
  const remedies = report?.remedies ?? []
  const partialTotals = Object.entries(partial.by_currency)

  return (
    <Card className="cash-coverage-card">
      <div className="panel-title">
        <div>
          <p className="eyebrow">EVIDENCE COVERAGE</p>
          <h2>
            {summary.status === 'complete'
              ? 'Every liquid pocket is proven'
              : summary.status === 'unavailable'
                ? 'No pocket has a coverage-backed balance'
                : `${summary.covered_pockets} of ${summary.included_pockets} liquid pockets proven`}
          </h2>
        </div>
        <Badge variant="outline" className={summary.status === 'complete' ? '' : 'review-badge'}>
          {percent}% covered
        </Badge>
      </div>

      <div
        className="coverage-meter" role="img"
        aria-label={`${percent}% of liquid pockets have a coverage-backed balance`}
      >
        <div className={`coverage-meter-fill is-${summary.status}`} style={{ width: `${percent}%` }} />
      </div>
      <p className="muted-note">{summary.basis}.</p>

      {partialTotals.length > 0 && !partial.is_complete && (
        <div className="coverage-partial">
          <p>
            <strong>Most current figure available</strong>
            {partial.as_of ? ` · as of ${partial.as_of}` : ''}:{' '}
            {partialTotals.map(([currency, value]) => money(value, currency)).join(' · ')}
          </p>
          <p className="muted-note">
            {partial.reason ?? 'Some pockets are not included.'} This is not your total cash — it is the
            total of the pockets that can be proven at that date. The complete total above stays dated to
            the last day every pocket proves.
          </p>
          {partial.short_pockets.length > 0 && (
            <ul className="issue-list">
              {partial.short_pockets.map(pocket => (
                <li key={`${pocket.account_id}:${pocket.currency_pocket}`}>
                  <strong>{pocket.display_name ?? pocket.account_id} · {pocket.currency_pocket}</strong>
                  {pocket.best_supported_cutoff && <> — proven only through {pocket.best_supported_cutoff}</>}
                  <p>{pocket.reason}</p>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {remedies.length > 0 && (
        <div className="coverage-remedies">
          <p className="eyebrow">WHAT WOULD CLOSE THE GAP</p>
          <ul className="remedy-list">
            {remedies.map((remedy, index) => {
              const Icon = ACTION_ICON[remedy.action]
              return (
                <li key={`${remedy.account_id}:${remedy.currency_pocket}:${remedy.action}:${index}`}>
                  <div className="remedy-body">
                    <p>
                      <strong>{remedy.display_name ?? remedy.account_id} · {remedy.currency_pocket}</strong>
                      {' '}<Badge variant="outline">{UNLOCKS_LABEL[remedy.unlocks]}</Badge>
                    </p>
                    <p className="muted-note">{remedy.detail}</p>
                  </div>
                  <Button
                    variant="outline" size="sm"
                    onClick={() => {
                      if (remedy.action === 'certify-quiet-interval') {
                        onCertify({
                          account_id: remedy.account_id,
                          currency_pocket: remedy.currency_pocket,
                          display_name: remedy.display_name,
                          period_start: remedy.parameters.period_start ?? report!.generated_for,
                          period_end: remedy.parameters.period_end ?? report!.generated_for,
                        })
                      } else if (remedy.action === 'record-balance-observation') {
                        onObserve(remedy.account_id, remedy.currency_pocket)
                      } else {
                        onImport()
                      }
                    }}
                  >
                    <Icon size={14} /> {ACTION_LABEL[remedy.action]}
                  </Button>
                </li>
              )
            })}
          </ul>
        </div>
      )}

      {remedies.length === 0 && summary.status === 'complete' && (
        <p className="muted-note"><ShieldCheck size={13} /> Nothing is outstanding. Every included pocket is proven through today.</p>
      )}

      {certifications.length > 0 && (
        <div className="coverage-certifications">
          <p className="eyebrow">RANGES YOU HAVE CERTIFIED AS QUIET</p>
          <ul className="issue-list">
            {certifications.map(certification => (
              <li key={certification.certification}>
                <div className="remedy-body">
                  <p>
                    <strong>{certification.display_name ?? certification.account_id} · {certification.currency_pocket}</strong>
                    {' '}· {certification.period_start} → {certification.period_end}
                  </p>
                  <p className="muted-note">{certification.basis.replace(/-/g, ' ')}</p>
                </div>
                <Button
                  variant="ghost" size="sm"
                  aria-label={`Retire the certification for ${certification.period_start} to ${certification.period_end}`}
                  onClick={() => onRetireCertification(certification)}
                >
                  <X size={14} /> Retire
                </Button>
              </li>
            ))}
          </ul>
          <p className="muted-note">
            Retiring a certification makes the days it covered unproven again, and any figure that
            depended on it is withheld immediately.
          </p>
        </div>
      )}
    </Card>
  )
}
