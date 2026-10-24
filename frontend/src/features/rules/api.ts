import { reportedApi } from '@/lib/workspaceReporting'
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
  reportedApi<CompiledRule>('rules/compile', { match, category, reason })

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
 * offer "save this as a reusable rule". Never auto-widens beyond what the backend's
 * `derive_rule_options` finds; `unavailable_reason` is non-null exactly when
 * `options` is empty. */
export type RuleOption = { label: string; match: Record<string, unknown> }
export type RuleOptionsPage = { generation: string; anchors: string[]; options: RuleOption[]; unavailable_reason: string | null }
export const deriveRuleOptions = (generation: string, anchors: string[]) =>
  api<RuleOptionsPage>('categorization/derive-rule', { generation, anchors })

/** A rename changes only the display name a merchant is shown under; the
 * canonical it matches against never changes, so every rule keeps matching.
 * Read-only: the
 * caller submits the returned `commands` through the existing `/batch`. */
export type MerchantRenamePreview = {
  generation: string; merchant_key: string; display_name: string
  commands: { kind: string; anchors: Record<string, string>; payload: Record<string, unknown> }[]
  affected_transactions: number
}
export const merchantRenamePreview = (generation: string, merchantKey: string, displayName: string, reason: string) =>
  api<MerchantRenamePreview>('categorization/merchant-rename/preview', {
    generation, merchant_key: merchantKey, display_name: displayName, reason,
  })

/** One row per `rule_id`, joining whichever of category/role/inbound rules
 * share a match predicate --
 * the grouped read model behind the combined rule form and the Rules page. */
export type GroupedRuleRow = {
  rule_id: string
  match: Record<string, unknown>
  match_label: string
  category: string | null
  cost_behavior: string | null
  need: string | null
  incoming_type: string | null
  kinds: Array<'category' | 'role' | 'inbound'>
  matches: number
  last_changed: string | null
  reason: string | null
}
export type GroupedRulesPage = { generation: string; grouped: GroupedRuleRow[] }
export const loadGroupedRules = (generation: string) => api<GroupedRulesPage>(`rules/grouped?generation=${encodeURIComponent(generation)}`)

export type CombinedRuleImpact = {
  category?: CompiledRule['impact']
  role?: RoleRuleImpact
  incoming_type?: { inbound_matches: number; still_unresolved: number }
}
export type CombinedRuleCommand = { kind: string; anchors: { rule_id: string }; payload: Record<string, unknown> }
export type CompiledCombinedRule = { rule_id: string; commands: CombinedRuleCommand[]; impact: CombinedRuleImpact }
export type CombinedRuleFields = { category?: string | null; cost_behavior?: string | null; need?: string | null; incoming_type?: string | null }
/** Preview 1-3 rule commands (category/role/inbound) that all share one
 * rule_id -- the server owns precedence and rule_id derivation, so this is a
 * pure pass-through to `/rules/compile-combined`. Leaving a field `null`
 * (rather than omitting it) retires that kind when it was already active. */
export const compileCombinedRule = (generation: string, match: Record<string, unknown>, fields: CombinedRuleFields, reason: string) =>
  reportedApi<CompiledCombinedRule>('rules/compile-combined', {
    generation, match,
    category: fields.category ?? null, cost_behavior: fields.cost_behavior ?? null,
    need: fields.need ?? null, incoming_type: fields.incoming_type ?? null, reason,
  })
export type CombinedRetirePreview = { rule_id: string; commands: CombinedRuleCommand[]; impact: CombinedRuleImpact }
export const retireCombinedPreview = (ruleId: string, generation: string, reason: string) =>
  reportedApi<CombinedRetirePreview>(`rules/${encodeURIComponent(ruleId)}/retire-combined/preview`, { generation, reason })

/** One observed merchant identity, aggregated across all-time ledger history --
 * the row shape shared by search
 * results and similarity suggestions. */
export type MerchantRow = {
  merchant_key: string
  display_name: string | null
  variants: string[]
  transaction_count: number
  last_seen: string | null
  has_rules: boolean
}
export type MerchantSearchPage = { generation: string; merchants: MerchantRow[] }
export const searchMerchants = (generation: string, query: string, limit = 20) =>
  api<MerchantSearchPage>(`merchants?generation=${encodeURIComponent(generation)}&q=${encodeURIComponent(query)}&limit=${limit}`)

export type MerchantSimilarPage = { generation: string; merchant_key: string; suggestions: MerchantRow[] }
export const similarMerchants = (generation: string, merchantKey: string) =>
  api<MerchantSimilarPage>(`merchants/similar?generation=${encodeURIComponent(generation)}&merchant_key=${encodeURIComponent(merchantKey)}`)

/** A merge conflict: two or more sources disagree on the outcome for one
 * rule kind (category / role / inbound). Resolve by naming the winning
 * source in `resolutions[kind]` and re-previewing -- neither the new rule
 * nor the old ones are emitted for that kind until it is resolved. */
export type MergeConflict = { kind: string; candidates: Array<{ from_merchant: string; value: unknown }> }
export type MerchantMergePreview = {
  generation: string; sources: string[]; target: string; patterns: Array<Record<string, string>>
  commands: Array<{ kind: string; anchors: Record<string, string>; payload: Record<string, unknown> }>
  conflicts: MergeConflict[]
  affected_transactions: number
  dependent_rules: number
}
export const merchantMergePreview = (
  generation: string, sources: string[], target: string, displayName: string | null, reason: string,
  resolutions: Record<string, string> = {},
) => api<MerchantMergePreview>('categorization/merchant-merge/preview', {
  generation, sources, target, display_name: displayName, reason, resolutions,
})
