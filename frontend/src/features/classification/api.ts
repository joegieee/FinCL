import { api } from '@/lib/api'

// Shared unified-classification client types
// (docs/PLAN-unified-transaction-classification.md Step 1/2). These mirror
// `fincl.classification`'s `ClassificationDraft`/`_summarize` shapes exactly;
// the backend route returns `Any`, so there is no generated schema type to
// import for the response.

export type FieldName = 'category' | 'cost_behavior' | 'need' | 'incoming_type'
export type FieldEdit = { intent: 'keep' | 'set'; value?: string | null }

export type ClassificationDraftBody = {
  generation: string
  anchors: string[]
  category?: FieldEdit
  cost_behavior?: FieldEdit
  need?: FieldEdit
  incoming_type?: FieldEdit
  clear_category?: boolean
  clear_roles?: boolean
  policy?: 'fill_missing' | 'replace'
  reason?: string
}

export type FieldOutcome = {
  applicable: boolean
  reason: string | null
  before: string | null
  after: string | null
  changed: boolean
  derived: boolean
  manual_replacement: boolean
  skipped_fill_missing: boolean
}

export type ClassificationCommand = { kind: string; anchors: Record<string, string>; payload: Record<string, unknown> }

export type ClassificationCurrencyTotal = {
  currency: string
  count: number
  changed: number
  absolute_amount: string
  category_changed: number
  behavior_changed: number
  need_changed: number
  incoming_changed: number
}

export type ClassificationRow = {
  anchor: string
  currency: string
  bucket: string
  fields: Record<FieldName, FieldOutcome>
}

export type ClassificationPreview = {
  generation: string
  changed_transactions: number
  command_count: number
  commands: ClassificationCommand[]
  by_currency: ClassificationCurrencyTotal[]
  fields: Record<FieldName, { applicable: number; not_applicable: number; changed: number; skipped_fill_missing: number; manual_replacements: number; derived: number }>
  rows: ClassificationRow[]
}

export function previewClassification(draft: ClassificationDraftBody) {
  return api<ClassificationPreview>('classification/preview', draft)
}

export const COST_BEHAVIORS = ['fixed_recurring', 'variable_routine', 'irregular', 'unresolved'] as const
export const NEEDS = ['essential', 'discretionary', 'unresolved'] as const
export const INCOMING_TYPES = ['income', 'reimbursement', 'gift', 'family_transfer', 'self_transfer', 'other'] as const
export const INCOMING_LABELS: Record<string, string> = {
  income: 'Income',
  reimbursement: 'Reimbursement',
  gift: 'Gift',
  family_transfer: 'Family transfer',
  self_transfer: 'Own-account transfer',
  other: 'Other',
}

export function label(field: FieldName | string, value: string): string {
  if (field === 'incoming_type') return INCOMING_LABELS[value] ?? value.replaceAll('_', ' ')
  return value.replaceAll('_', ' ').replace(/^./, s => s.toUpperCase())
}

/**
 * Bucket-driven applicability, mirroring `fincl.classification._applicability`
 * exactly so the editor never offers a field the server would reject.
 * `entry.bucket === 'internal'` alone is ambiguous: a confirmed relationship
 * (`linked`) owns the movement and exposes nothing, while an unlinked
 * `self_transfer` classification must keep its incoming type editable.
 */
export function detailBucket(entry: { bucket: string; linked: boolean }): string {
  if (entry.bucket !== 'internal') return entry.bucket
  return entry.linked ? 'internal_linked' : 'internal_self_transfer'
}

export type Applicability = { category: boolean; cost_behavior: boolean; need: boolean; incoming_type: boolean }

const INCOMING_ELIGIBLE_BUCKETS = new Set([
  'internal_self_transfer', 'income', 'reimbursements', 'non_income_inflow', 'refunds', 'pending_review', 'other',
])

export function applicability(bucket: string): Applicability {
  // A personal debt owns a movement it fully explains (`debt`), and a confirmed
  // installment plan its instalments and conversion credit (`installment`), the
  // way a confirmed transfer link owns an internal one: nothing here applies.
  if (bucket === 'excluded' || bucket === 'debt' || bucket === 'installment') return { category: false, cost_behavior: false, need: false, incoming_type: false }
  return {
    category: bucket !== 'internal_linked' && bucket !== 'internal_self_transfer',
    cost_behavior: bucket === 'spending',
    need: bucket === 'spending',
    incoming_type: INCOMING_ELIGIBLE_BUCKETS.has(bucket),
  }
}
