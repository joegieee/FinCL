import { api } from '@/lib/api'

export type AliasRow = {
  canonical: string
  patterns: { exact?: string; prefix?: string; regex?: string }[]
  display_name: string | null
  reason: string | null
  journal_seq: number
  metrics: { observed_matches: number; effective_assignments: number; first_match: string | null; last_match: string | null }
}

export type RuleRow = {
  rule_id: string
  match: Record<string, unknown>
  group_name: string
  category: string
  reason: string | null
  journal_seq: number
  metrics: { predicate_matches: number; effective_assignments: number; overridden: number; direction_conflicts: number; first_match: string | null; last_match: string | null }
}

export type RulesPage = { generation: string; aliases: AliasRow[]; rules: RuleRow[]; inactive_rules: RuleRow[] }

export type CompiledRule = {
  command: { kind: 'CATEGORY_RULE_SET'; anchors: { rule_id: string }; payload: Record<string, unknown> }
  match_count: number
  impact: {
    predicate_matches: number
    effective_assignments: number
    newly_categorized: number
    recategorized: number
    protected_by_override: number
    inactive?: number
    ambiguous?: number
    direction_conflict: number
    before_coverage?: Record<string, number>
    after_coverage?: Record<string, number>
    breakdowns?: { matched: { by_currency: Record<string, { count: number; absolute_amount: string }>; by_direction: Record<string, number>; by_account: Record<string, number>; resulting_category: Record<string, number> }; changed: { by_currency: Record<string, { count: number; absolute_amount: string }> } }
    overlaps?: Array<{ rule_id: string; observed_matches: number; kind: string; message: string }>
    warnings?: string[]
  }
}

export const PREDICATES = [
  { key: 'merchant_canonical', label: 'Merchant (canonical name)' },
  { key: 'description_prefix', label: 'Description starts with' },
  { key: 'description_contains', label: 'Description contains' },
  { key: 'paynow_to', label: 'PayNow recipient' },
] as const

export const loadRules = () => api<RulesPage>('rules')
export const compileRule = (match: Record<string, unknown>, category: string, reason: string) =>
  api<CompiledRule>('rules/compile', { match, category, reason })
