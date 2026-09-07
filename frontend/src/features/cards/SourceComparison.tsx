import { money } from '@/lib/api'
import { RECONCILIATION_LABEL, type PocketView } from './api'

// Reusable: render one statement pocket as the issuer's own source equation
// (opening liability + purchases + fees/interest − repayments − credits =
// closing liability) using the printed bucket letters, plus its reconciliation
// status and every recorded exact check. Never invents a sign from a merchant
// label — the letters and their signs are the adapter's.
const BUCKET_LABEL: Record<string, string> = {
  A: 'Previous balance (opening)',
  B: 'Repayments / conversions',
  C: 'Waivers / refunds',
  D: 'Purchases',
  E: 'Cash / loans',
  F: 'Cashback',
  G: 'Bank charges',
  H: 'Split payments',
  I: 'Others',
}

export function SourceComparison({ pocket }: { pocket: PocketView }) {
  const recon = pocket.reconciliation
  return (
    <div className="card-source">
      <div className="card-source-head">
        <span>{pocket.currency}</span>
        <small>
          {RECONCILIATION_LABEL[recon.status] ?? recon.status}
          {recon.printed_equation ? ` · equation ${recon.printed_equation}` : ''}
          {recon.blocking_total > 0
            ? ` · ${recon.blocking_passed}/${recon.blocking_total} checks passed`
            : ''}
        </small>
      </div>
      <table className="card-source-table">
        <tbody>
          <tr>
            <th>Opening liability</th>
            <td>
              {pocket.opening_liability
                ? money(pocket.opening_liability.value, pocket.currency)
                : 'Not evidenced'}
            </td>
          </tr>
          {Object.entries(pocket.summary_buckets).map(([letter, fact]) => (
            <tr key={letter}>
              <th>
                {BUCKET_LABEL[letter] ?? `Bucket ${letter}`}
              </th>
              <td>{money(fact.value, pocket.currency)}</td>
            </tr>
          ))}
          <tr className="card-source-total">
            <th>Closing liability</th>
            <td>
              {pocket.closing_liability
                ? money(pocket.closing_liability.value, pocket.currency)
                : 'Not evidenced'}
            </td>
          </tr>
        </tbody>
      </table>
      {recon.checks.length > 0 && (
        <table className="card-source-checks">
          <thead>
            <tr><th>Check</th><th>Expected</th><th>Actual</th><th>Δ</th><th /></tr>
          </thead>
          <tbody>
            {recon.checks.map((c, i) => (
              <tr key={i}>
                <td>{c.check_type}</td>
                <td>{c.expected ?? '—'}</td>
                <td>{c.actual ?? '—'}</td>
                <td>{c.difference ?? '—'}</td>
                <td>{c.passed === null ? '·' : c.passed ? '✓' : '✗'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
