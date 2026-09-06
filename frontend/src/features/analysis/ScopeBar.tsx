import { X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { shortCategory } from '@/lib/api'
import { type AnalysisFilters, emptyFilters, filtersActive } from './api'

/**
 * The Analysis-local scope strip. Period and currency stay in the shared
 * scope bar above; this row owns the comparison mode and the account /
 * category / merchant narrowing, which are visibly local to Analysis and
 * reset here without touching Overview.
 */
export function ScopeBar({
  filters, setFilters, accounts, categories, comparisonKind,
}: {
  filters: AnalysisFilters
  setFilters: (f: AnalysisFilters) => void
  accounts: string[]
  categories: string[]
  comparisonKind: string
}) {
  const active = filtersActive(filters)
  const comparisonLabel: Record<string, string> = {
    auto: comparisonKind === 'previous_month' ? 'Previous month' : comparisonKind === 'none' ? 'No comparison' : 'Equal period before',
    previous_month: 'Previous month',
    preceding_equal_duration: 'Equal period before',
    none: 'No comparison',
  }
  return (
    <div className="analysis-filters">
      <label>
        Compare with
        <select
          aria-label="Comparison period"
          value={filters.comparison}
          onChange={e => setFilters({ ...filters, comparison: e.target.value as AnalysisFilters['comparison'] })}
        >
          <option value="auto">Automatic ({comparisonLabel.auto})</option>
          <option value="previous_month">Previous complete month</option>
          <option value="preceding_equal_duration">Equal-length period before</option>
          <option value="none">No comparison</option>
        </select>
      </label>
      <label>
        Account
        <select aria-label="Account scope" value={filters.account} onChange={e => setFilters({ ...filters, account: e.target.value })}>
          <option value="">All accounts</option>
          {accounts.map(a => <option key={a}>{a}</option>)}
        </select>
      </label>
      <label>
        Category
        <select aria-label="Category scope" value={filters.category} onChange={e => setFilters({ ...filters, category: e.target.value })}>
          <option value="">All categories</option>
          {categories.map(c => <option key={c} value={c}>{shortCategory(c)}</option>)}
        </select>
      </label>
      <label>
        Merchant
        <input
          aria-label="Merchant scope"
          placeholder="Exact merchant key"
          value={filters.merchant}
          onChange={e => setFilters({ ...filters, merchant: e.target.value })}
        />
      </label>
      {active && (
        <Button variant="ghost" size="sm" onClick={() => setFilters(emptyFilters)}>
          <X size={14} /> Reset analysis filters
        </Button>
      )}
    </div>
  )
}
