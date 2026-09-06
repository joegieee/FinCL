import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowRight } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { money, shortCategory, type Analysis } from '@/lib/api'
import { type AnalysisFilters, emptyFilters, filtersActive, loadAnalysis } from './api'
import { ScopeBar } from './ScopeBar'
import { ComparisonCards } from './ComparisonCards'
import { TrendChart } from './TrendChart'
import { ChangeRanking } from './ChangeRanking'
import { ContributorTable } from './ContributorTable'

type Selected = { title: string; handle: string } | null

/**
 * Analysis view (v0.2 slice 2). One `/api/v1/analysis` read drives every panel,
 * so a total and its drill-down cannot disagree. Filters are local to this
 * screen; opening Transactions copies the category / merchant narrowing.
 */
export function AnalysisView({
  generation, start, end, currency, accounts, categories, onOpenTransactions,
}: {
  generation: string
  start: string
  end: string
  currency: string
  accounts: string[]
  categories: string[]
  onOpenTransactions: (category: string, merchant: string) => void
}) {
  const [filters, setFilters] = useState<AnalysisFilters>(emptyFilters)
  const [analysis, setAnalysis] = useState<Analysis | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<Selected>(null)
  const seq = useRef(0)

  const fetchAnalysis = useCallback(() => {
    const id = ++seq.current
    setLoading(true); setError('')
    loadAnalysis({ start, end, currency, generation, filters })
      .then(a => { if (id === seq.current) { setAnalysis(a); setSelected(null) } })
      .catch(e => { if (id === seq.current) { setError((e as Error).message); setAnalysis(null) } })
      .finally(() => { if (id === seq.current) setLoading(false) })
  }, [start, end, currency, generation, filters])

  useEffect(() => { fetchAnalysis() }, [fetchAnalysis])

  return (
    <div className="analysis-view" aria-busy={loading}>
      <ScopeBar
        filters={filters}
        setFilters={setFilters}
        accounts={accounts}
        categories={categories}
        comparisonKind={analysis?.comparison.kind ?? 'auto'}
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

      {analysis && !analysis.covered && (
        <div className="alert" role="status">
          <div>
            <strong>This period has missing coverage.</strong>
            <p>Totals and comparisons are unavailable. Choose a covered period or import the missing statements.</p>
          </div>
        </div>
      )}
      {analysis && analysis.covered && !analysis.comparison.available && analysis.comparison.kind !== 'none' && (
        <div className="alert" role="status">
          <div>
            <strong>No comparison available.</strong>
            <p>{analysis.comparison.reasons[0] ?? 'The comparison period is not fully covered.'}</p>
          </div>
        </div>
      )}
      {analysis && analysis.freshness === 'stale' && (
        <div className="alert" role="status">New ledger inputs are available. Refresh before interpreting these totals as current.</div>
      )}

      {analysis && analysis.covered && (
        <>
          <ComparisonCards analysis={analysis} currency={currency} />
          <TrendChart trend={analysis.trend} currency={currency} />
          <div className="analysis-grid">
            <ChangeRanking
              analysis={analysis}
              currency={currency}
              onDrill={row => row.handle && setSelected({ title: `${shortCategory(row.category)} — spending`, handle: row.handle })}
            />
            <Card className="ranking">
              <h2>Who gets most of my money?</h2>
              {analysis.merchant_ranking.length
                ? analysis.merchant_ranking.slice(0, 12).map((m, i) => (
                    <button
                      className="rank-row"
                      key={m.merchant}
                      onClick={() => m.handle && setSelected({ title: `${m.merchant} — spending`, handle: m.handle })}
                    >
                      <span className="rank-label"><span>{m.merchant}</span><b>{money(m.value.value, currency)}</b></span>
                      <span className="rank-track">
                        <span style={{
                          width: `${Number(m.value.value) / Number(analysis.merchant_ranking[0].value.value) * 100}%`,
                          background: ['#247b69', '#64a994', '#accbb3', '#c8aa70', '#809ba6'][i % 5],
                        }} />
                      </span>
                      <small>{m.count} transactions <ArrowRight size={12} /></small>
                    </button>
                  ))
                : <div className="empty"><p>No spending in this period.</p></div>}
            </Card>
          </div>

          {selected
            ? <ContributorTable title={selected.title} handle={selected.handle} currency={currency} onClose={() => setSelected(null)} />
            : analysis.contributors_handle && (
                <Card className="question-card">
                  <h2>Go a little deeper</h2>
                  <div className="question-links">
                    <button onClick={() => setSelected({ title: 'All spending — contributors', handle: analysis.contributors_handle! })}>
                      Show every component behind spending <ArrowRight size={16} />
                    </button>
                    <button onClick={() => analysis.summary_handles.refunds && setSelected({ title: 'Refunds — contributors', handle: analysis.summary_handles.refunds })}>
                      Which purchases were refunded? <ArrowRight size={16} />
                    </button>
                    <button onClick={() => analysis.summary_handles.pending_review && setSelected({ title: 'Pending review — contributors', handle: analysis.summary_handles.pending_review })}>
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
