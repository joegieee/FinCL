import { api, type Analysis, type ContributorsPage } from '@/lib/api'

export type AnalysisFilters = {
  comparison: 'auto' | 'previous_month' | 'preceding_equal_duration' | 'none'
  account: string
  category: string
  merchant: string
}

export const emptyFilters: AnalysisFilters = { comparison: 'auto', account: '', category: '', merchant: '' }

export function filtersActive(f: AnalysisFilters): boolean {
  return f.comparison !== 'auto' || !!f.account || !!f.category || !!f.merchant
}

export function loadAnalysis(
  opts: { start: string; end: string; currency: string; generation: string; filters: AnalysisFilters },
): Promise<Analysis> {
  const q = new URLSearchParams({
    start: opts.start,
    end: opts.end,
    currency: opts.currency,
    expected_generation: opts.generation,
  })
  if (opts.filters.comparison !== 'auto') q.set('comparison', opts.filters.comparison)
  if (opts.filters.account) q.append('accounts', opts.filters.account)
  if (opts.filters.category) q.append('category_keys', opts.filters.category)
  if (opts.filters.merchant) q.set('merchant', opts.filters.merchant)
  return api<Analysis>('analysis?' + q)
}

export function loadContributors(handle: string, cursor?: string | null): Promise<ContributorsPage> {
  const q = new URLSearchParams({ limit: '100' })
  if (cursor) q.set('cursor', cursor)
  return api<ContributorsPage>(`contributors/${encodeURIComponent(handle)}?` + q)
}
