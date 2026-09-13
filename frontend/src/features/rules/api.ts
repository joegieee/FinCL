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

export type RoleRuleRow = {
  rule_id: string
  match: Record<string, unknown>
  cost_behavior: string | null
  need: string | null
  reason: string
  journal_seq: number
}
export type RoleRulesPage = { generation: string | null; rules: RoleRuleRow[] }
export const loadRoleRules = () => api<RoleRulesPage>('financial-roles/rules')

export const loadRules = () => api<RulesPage>('rules')
export const compileRule = (match: Record<string, unknown>, category: string, reason: string) =>
  api<CompiledRule>('rules/compile', { match, category, reason })

export type RoleRuleImpact = { matches: number; will_change: number; protected_overrides: number }
export type CompiledRoleRule = {
  command: { kind: 'ROLE_RULE_SET'; anchors: { rule_id: string }; payload: Record<string, unknown> }
  impact: RoleRuleImpact
}
export const compileRoleRule = (
  generation: string, match: Record<string, unknown>, cost_behavior: string | null, need: string | null, reason: string,
) => api<CompiledRoleRule>('financial-roles/rules/compile', { generation, match, cost_behavior, need, reason })

/** A closed, safe-predicate suggestion derived from a frozen anchor selection
 * -- shared by grouped categorization and any other selection that wants to
 * offer "save this as a reusable rule" (PLAN-unified-transaction-
 * classification.md Step 7). Never auto-widens beyond what the backend's
 * `derive_rule_options` finds; `unavailable_reason` is non-null exactly when
 * `options` is empty. */
export type RuleOption = { label: string; match: Record<string, unknown> }
export type RuleOptionsPage = { generation: string; anchors: string[]; options: RuleOption[]; unavailable_reason: string | null }
export const deriveRuleOptions = (generation: string, anchors: string[]) =>
  api<RuleOptionsPage>('categorization/derive-rule', { generation, anchors })
