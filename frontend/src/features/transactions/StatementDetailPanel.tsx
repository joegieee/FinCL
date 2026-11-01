import { Money } from '@/components/Money'
import type { TransactionRowOut, ValidationOut } from './reportedApi'

// §5.3: a mismatch/conflict/not-checkable *statement* validation is an
// informational note about the optional printed-rate detail, never a claim
// that the reconciled native ledger entry is wrong.
const VALIDATION_NOTE: Record<ValidationOut['status'], string> = {
  matches: 'The printed rate matches the derived ratio (within the documented rounding allowance).',
  mismatch: 'The printed rate differs from the derived ratio beyond the documented rounding allowance. This is a note about the optional printed statement detail only — it does not call the recorded native amount into question.',
  not_checkable: 'The printed rate could not be checked against a derived ratio (direction, precision or fee treatment not known).',
  conflicting_fields: 'The stored and decoded original amounts disagree. The stored value is shown and kept authoritative; nothing was silently replaced.',
}

function exact(amount: { currency: string; amount_text: string } | null, whenMissing: string): string {
  return amount ? `${amount.currency} ${amount.amount_text}` : whenMissing
}

/**
 * §5.3's statement-money facts, revealed inside the existing transaction
 * dialog rather than a new top-level workspace. Every amount here is a
 * distinct, separately labelled fact — never merged into one figure.
 */
export function StatementDetailPanel({ row }: { row: TransactionRowOut }) {
  const d = row.statement_detail
  return (
    <div className="statement-detail-panel">
      <span className="eyebrow">Original statement detail</span>
      <dl className="detail-list">
        <div><dt>Original purchase</dt><dd>{exact(d.original, 'Not recoverable from the statement')}</dd></div>
        <div><dt>Native settlement</dt><dd>{d.settled.currency} {d.settled.amount_text}</dd></div>
        <div><dt>Explicit fee</dt><dd>{exact(d.explicit_fee, 'None recorded separately')}</dd></div>
        <div><dt>Supported cash movement</dt><dd>{exact(d.statement_cash_movement, 'Not available')}</dd></div>
        {d.printed_conversion && (
          <div><dt>Printed provider/benchmark rate</dt><dd><code>{d.printed_conversion.raw_text}</code></dd></div>
        )}
        {d.printed_charge_percent && <div><dt>Printed charge %</dt><dd>{d.printed_charge_percent}</dd></div>}
        {d.effective_rate && (
          <div>
            <dt>Derived effective rate</dt>
            <dd>
              1 {d.effective_rate.base_currency} = {d.effective_rate.numerator}/{d.effective_rate.denominator} {d.effective_rate.quote_currency}
              {' '}({d.effective_rate.amount_basis.replaceAll('_', ' ')})
              <br /><small>Derived from settled/original amounts — not a printed statement fact.</small>
            </dd>
          </div>
        )}
        <div><dt>Reported equivalent</dt><dd><Money value={row.amount} /></dd></div>
        {row.fee && <div><dt>Reported fee</dt><dd><Money value={row.fee} /></dd></div>}
      </dl>
      <p className="muted-note">{VALIDATION_NOTE[d.validation.status] ?? d.validation.reason ?? ''}</p>
      <small>
        Source: {d.provenance.detail_basis.replaceAll('_', ' ')}
        {d.provenance.source_location ? ` · ${d.provenance.source_location}` : ''}
      </small>
    </div>
  )
}
