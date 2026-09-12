import { ArrowRight } from 'lucide-react'
import { Card } from '@/components/ui/card'
import { ConsolidatedMoney } from '@/components/Money'
import { shortCategory } from '@/lib/api'
import type { RankedGroupOut } from './reportedApi'

/**
 * Top spending categories, all pockets consolidated before ranking (§3.1:
 * "do not concatenate each pocket's top N"). The reported backend does not
 * yet compute a per-category current-vs-previous delta the way the retired
 * native "What changed?" panel did (that comparison never had a reported
 * equivalent built in step 04) — this is recorded as a known, deliberate
 * simplification for this step rather than left silently dropped.
 */
export function ChangeRanking({
  ranking, currency, onDrill,
}: {
  ranking: RankedGroupOut[]
  currency: string
  onDrill: (row: RankedGroupOut) => void
}) {
  if (!ranking.length) {
    return (
      <Card className="ranking">
        <h2>Top spending categories</h2>
        <div className="empty"><p>No spending in this period.</p></div>
      </Card>
    )
  }
  return (
    <Card className="ranking change-ranking">
      <h2>Top spending categories</h2>
      <p className="restate-note">Consolidated across every pocket, then ranked — reporting currency</p>
      <div className="change-columns">
        <div>
          {ranking.map(row => (
            <button className="change-row" key={row.name} onClick={() => onDrill(row)}>
              <span className="change-name">{shortCategory(row.name)}</span>
              <span className="change-values">
                <b><ConsolidatedMoney
                  total={row.result.total} partialTotal={row.result.partial_total} currency={currency}
                  unavailableReasons={row.result.unavailable_reasons} excludedDetail={row.result.excluded_detail}
                /></b>
              </span>
              <small className="change-detail">Show contributors <ArrowRight size={11} /></small>
            </button>
          ))}
        </div>
      </div>
    </Card>
  )
}
