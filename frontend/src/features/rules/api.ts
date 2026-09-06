import { api } from '@/lib/api'

export type AliasRow = {
  canonical: string
  patterns: { exact?: string; prefix?: string; regex?: string }[]
  display_name: string | null
  reason: string | null
  journal_seq: number
}

export type RuleRow = {
  rule_id: string
  match: Record<string, unknown>
  group_name: string
  category: string
  reason: string | null
  journal_seq: number
}

export type RulesPage = { generation: string; aliases: AliasRow[]; rules: RuleRow[] }

export type CompiledRule = {
  command: { kind: 'CATEGORY_RULE_SET'; anchors: { rule_id: string }; payload: Record<string, unknown> }
  match_count: number
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
