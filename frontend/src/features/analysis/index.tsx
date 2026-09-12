import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowRight } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { ApiError, shortCategory } from '@/lib/api'
import { ConsolidatedMoney } from '@/components/Money'
import {
  type AnalysisFilters, type ReportedAnalysis, type ReportingPolicySummary,
  emptyFilters, filtersActive, loadReportedAnalysis, loadReportingPolicy, scopeKey,
} from './reportedApi'
import { ScopeBar } from './ScopeBar'
import { ComparisonCards } from './ComparisonCards'
import { TrendChart } from './TrendChart'
import { ChangeRanking } from './ChangeRanking'
import { ContributorTable } from './ContributorTable'

type Selected = { title: string; handle: string } | null
type StaleBanner = 'none' | 'same-scope' | 'refresh-required'
type LastGood = { key: string; policyCurrency: string; response: ReportedAnalysis }

/**
 * Analysis view (M3b.1 step 05). One `GET /api/v1/reporting/analysis` read
 * drives every panel over *all* pockets by default, so a total and its
 * drill-down cannot disagree and no screen-local currency predicate can
 * silently narrow it. Reporting currency is shown as a setting
 * (`reporting_currency` on the response); the only record filter this screen
 * owns is the explicit, locally-visible Account-currency (`pocket_currency`)
 * filter in its own scope bar — never the ledger's reporting-currency
 * setting, and never the native Dashboard's pocket-filtered money (§6.3).
 */
export function AnalysisView({
  start, end, accounts, categories, currencies, initialPocketCurrency, onOpenTransactions,
  onOpenTransactionAnchor, onOpenDebts,
}: {
  start: string
  end: string
  accounts: string[]
  categories: string[]
  currencies: string[]
  // §6.3: a legacy `?currency=USD` URL is not a reporting-currency change —
  // it seeds this screen's own explicit, clearable Account-currency filter
  // once, on first mount, exactly the way the old pocket-filtered Dashboard
  // URL already behaved. Later changes to this screen's filter or to the
  // app's other native-tab currency selector no longer affect one another.
  initialPocketCurrency: string
  onOpenTransactions: (category: string, merchant: string) => void
  // M3b.2 step 08: exact transaction/owner drill-through from a contributor
  // row (§5.3, §6.3 "safe anchor encoding").
  onOpenTransactionAnchor: (encodedAnchor: string) => Promise<void>
  onOpenDebts: () => void
}) {
  const [filters, setFilters] = useState<AnalysisFilters>(() => ({ ...emptyFilters, pocketCurrency: initialPocketCurrency }))
  const [analysis, setAnalysis] = useState<ReportedAnalysis | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [staleBanner, setStaleBanner] = useState<StaleBanner>('none')
  const [selected, setSelected] = useState<Selected>(null)
  const [policy, setPolicy] = useState<ReportingPolicySummary | null>(null)
  const lastGood = useRef<LastGood | null>(null)
  const seq = useRef(0)

  useEffect(() => { loadReportingPolicy().then(setPolicy).catch(() => {}) }, [])

  const fetchAnalysis = useCallback(() => {
    const id = ++seq.current
    const key = scopeKey({ historyStart: start, historyEnd: end, filters })
    setLoading(true); setError('')
    loadReportedAnalysis({ historyStart: start, historyEnd: end, filters })
      .then(response => {
        if (id !== seq.current) return
        lastGood.current = { key, policyCurrency: response.reporting_currency, response }
        setAnalysis(response); setStaleBanner('none'); setSelected(null)
      })
      .catch(async caught => {
        if (id !== seq.current) return
        if (caught instanceof ApiError && caught.status === 409) {
          const saved = lastGood.current
          // Same requested scope: show the saved response with its own
          // provenance, never a different scope's values (§6.2, item 6).
          if (saved && saved.key === key) {
            // A reporting-policy change is a *stronger* invalidation than
            // ordinary staleness: verify the resolved policy currency still
            // matches what the saved response was valued under before
            // reusing it, clearing it otherwise (§6.3 "clear it on policy change").
            try {
              const current = await loadReportingPolicy()
              if (id !== seq.current) return
              setPolicy(current)
              if (current.currency === saved.policyCurrency) {
                setAnalysis(saved.response); setStaleBanner('same-scope'); return
              }
            } catch { /* fall through to refresh-required below */ }
          }
          lastGood.current = null
          setAnalysis(null); setStaleBanner('refresh-required')
          return
        }
        setError((caught as Error).message); setAnalysis(null); setStaleBanner('none')
      })
      .finally(() => { if (id === seq.current) setLoading(false) })
  }, [start, end, filters])

  useEffect(() => { fetchAnalysis() }, [fetchAnalysis])

  const reportingCurrency = analysis?.reporting_currency ?? policy?.currency ?? 'SGD'

  return (
    <div className="analysis-view" aria-busy={loading}>
      <div className="scope-caption">
        <span>Reporting in {reportingCurrency}{policy?.is_default && ' (ledger default)'}</span>
        <Button variant="ghost" size="sm" disabled={loading} onClick={fetchAnalysis}>Refresh</Button>
      </div>
      <ScopeBar
        filters={filters}
        setFilters={setFilters}
        accounts={accounts}
        categories={categories}
        currencies={currencies}
        comparisonKind={analysis?.comparison_kind ?? 'auto'}
      />
      {filtersActive(filters) && (
        <p className="analysis-scope-note">
          Filters apply only to Analysis.{' '}
          {(filters.category || filters.merchant) && (
            <button
              className="linkish"
              onClick={() => onOpenTransactions(filters.category, filters.merchant)}
            >
              Open these in Transactions <ArrowRight size={12} />
            </button>
          )}
        </p>
      )}

      {error && (
        <div className="alert error" role="alert">
          <div><strong>Couldn’t load analysis.</strong><p>{error}</p></div>
          <Button variant="outline" onClick={fetchAnalysis}>Try again</Button>
        </div>
      )}

      {staleBanner === 'refresh-required' && (
        <div className="alert" role="alert">
          <div>
            <strong>Refresh required.</strong>
            <p>The ledger has changed since the last refresh (new evidence, a rate import, or a reporting-policy change). Refresh before reading this view.</p>
          </div>
          <Button variant="outline" onClick={fetchAnalysis}>Refresh</Button>
        </div>
      )}
      {staleBanner === 'same-scope' && analysis && (
        <div className="alert" role="status">
          <div>
            <strong>Showing the last loaded values for this exact period and filters.</strong>
            <p>New ledger inputs are available. Refresh to recompute.</p>
          </div>
          <Button variant="outline" onClick={fetchAnalysis}>Refresh</Button>
        </div>
      )}

      {analysis && !analysis.covered && (
        <div className="alert" role="status">
          <div>
            <strong>This period has missing coverage.</strong>
            <p>Totals and comparisons are unavailable. Choose a covered period or import the missing statements.</p>
          </div>
        </div>
      )}
      {analysis && analysis.covered && !analysis.summary.spending.comparison_available && analysis.comparison_kind !== 'none' && (
        <div className="alert" role="status">
          <div>
            <strong>No comparison available.</strong>
            <p>{analysis.summary.spending.comparison_reasons[0] ?? 'The comparison period is not fully covered.'}</p>
          </div>
        </div>
      )}

      {analysis && (
        <>
          <ComparisonCards analysis={analysis} />
          <TrendChart trend={analysis.trend} currency={reportingCurrency} range={filters.trendRange} />
          <div className="analysis-grid">
            <ChangeRanking
              ranking={analysis.category_ranking}
              currency={reportingCurrency}
              onDrill={row => setSelected({ title: `${shortCategory(row.name)} — spending`, handle: row.handle })}
            />
            <Card className="ranking">
              <h2>Spending by merchant</h2>
              {analysis.merchant_ranking.length
                ? analysis.merchant_ranking.map((m, i) => {
                    const top = analysis.merchant_ranking[0].result.total ?? analysis.merchant_ranking[0].result.partial_total
                    const mine = m.result.total ?? m.result.partial_total
                    const width = top && mine ? Number(mine.value) / Number(top.value) * 100 : 0
                    return (
                      <button
                        className="rank-row"
                        key={m.name}
                        onClick={() => setSelected({ title: `${m.name} — spending`, handle: m.handle })}
                      >
                        <span className="rank-label">
                          <span>{m.name}</span>
                          <b><ConsolidatedMoney
                            total={m.result.total} partialTotal={m.result.partial_total} currency={reportingCurrency}
                            unavailableReasons={m.result.unavailable_reasons} excludedDetail={m.result.excluded_detail}
                          /></b>
                        </span>
                        <span className="rank-track">
                          <span style={{
                            width: `${width}%`,
                            background: ['#247b69', '#64a994', '#accbb3', '#c8aa70', '#809ba6'][i % 5],
                          }} />
                        </span>
                      </button>
                    )
                  })
                : <div className="empty"><p>No spending in this period.</p></div>}
            </Card>
          </div>

          {selected
            ? <ContributorTable title={selected.title} handle={selected.handle} onClose={() => setSelected(null)} onOpenTransaction={onOpenTransactionAnchor} onOpenDebts={onOpenDebts} />
            : analysis.contributors_handle && (
                <Card className="question-card">
                  <h2>Go a little deeper</h2>
                  <div className="question-links">
                    <button onClick={() => setSelected({ title: 'All spending — contributors', handle: analysis.contributors_handle })}>
                      Show every component behind spending <ArrowRight size={16} />
                    </button>
                    <button onClick={() => setSelected({ title: 'Refunds — contributors', handle: analysis.summary.refunds.handle })}>
                      Which purchases were refunded? <ArrowRight size={16} />
                    </button>
                    <button onClick={() => setSelected({ title: 'Pending review — contributors', handle: analysis.summary.pending_review.handle })}>
                      Which incoming payments need classification? <ArrowRight size={16} />
                    </button>
                    <button onClick={() => onOpenTransactions('', '')}>
                      Open the full transaction list <ArrowRight size={16} />
                    </button>
                  </div>
                </Card>
              )}
        </>
      )}
    </div>
  )
}
