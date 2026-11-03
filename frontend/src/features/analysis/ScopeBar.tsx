import { X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { shortCategory } from '@/lib/api'
import { type AnalysisFilters, emptyFilters, filtersActive } from './reportedApi'

/**
 * The Analysis-local scope strip (§6.3). Period stays in the shared scope bar
 * above; everything here — comparison mode, chart history, the explicit
 * Account-currency filter, and account/category/merchant narrowing — is
 * visibly local to Analysis and resets here without touching Overview or the
 * ledger's reporting-currency setting. Reporting currency is shown as a
 * setting, never as a record filter: it has no control in this row at all.
 */
export function ScopeBar({
  filters, setFilters, accounts, categories, currencies, comparisonKind,
}: {
  filters: AnalysisFilters
  setFilters: (f: AnalysisFilters) => void
  accounts: string[]
  categories: string[]
  currencies: string[]
  comparisonKind: string
}) {
  const active = filtersActive(filters)
  const comparisonLabel: Record<string, string> = {
    auto: comparisonKind === 'previous_month' ? 'Previous month' : comparisonKind === 'none' ? 'No comparison' : comparisonKind === 'preceding_months' ? 'Same number of months before' : 'Equal period before',
    previous_month: 'Previous month',
    preceding_months: 'Same number of months before',
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
          <option value="preceding_months">Same number of months before</option>
          <option value="preceding_equal_duration">Equal-length period before</option>
          <option value="none">No comparison</option>
        </select>
      </label>
      <label>
        Chart history
        <select
          aria-label="Chart history"
          value={filters.trendRange}
          onChange={e => setFilters({ ...filters, trendRange: e.target.value as AnalysisFilters['trendRange'] })}
        >
          <option value="6">Last 6 months</option>
          <option value="12">Last 12 months</option>
          <option value="all">All covered history</option>
        </select>
      </label>
      <label>
        Account currency
        <select
          aria-label="Account currency filter"
          value={filters.pocketCurrency}
          onChange={e => setFilters({ ...filters, pocketCurrency: e.target.value })}
        >
          <option value="">All pockets</option>
          {currencies.map(c => <option key={c}>{c}</option>)}
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
