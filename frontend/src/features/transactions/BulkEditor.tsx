import { useEffect, useState } from 'react'
import { Layers3, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { api, shortCategory } from '@/lib/api'
import { useBatchCommand } from '@/lib/command'
import { rememberCategory } from '@/features/classification/CategoryPicker'
import { ClassificationPreviewSummary } from '@/features/classification/PreviewSummary'
import { ClassificationFieldsFieldset } from '@/features/classification/FieldsFieldset'
import { PresetPicker } from '@/features/classification/PresetPicker'
import type { PresetFields } from '@/features/classification/presets'
import { RuleSection } from '@/features/classification/RuleSection'
import {
  applicability, detailBucket, label,
  previewClassification, type ClassificationDraftBody, type ClassificationPreview, type FieldName,
} from '@/features/classification/api'
import type { TransactionRowOut } from './reportedApi'

const UNCLASSIFIED = 'Unclassified / Unclassified'

type RawFilters = {
  start: string; end: string; pocket_currency?: string; search: string; category: string; account: string
  kind: string; direction: string; behavior: string; need: string; basis: string
  unresolved_only: boolean; include_excluded: boolean
}

type Scope = { anchors: string[]; truncated: boolean }

/**
 * Bulk classification through the shared editor.
 * Replaces the old
 * BulkAssign form: the same category/pattern/need/incoming-type fields, the
 * same Step 1 combined preview, and the same retry-safe batch lifecycle as
 * the single-transaction editor (Step 2), applied to a frozen selection
 * instead of one transaction. Every edit starts at "Keep existing values";
 * choosing a field value only ever emits a decision for transactions where
 * that field applies, disclosed exactly by the preview below.
 */
export function BulkEditor({
  generation, freshness, categories, rows, picked, matchAll, total, filters,
  onSelectPage, onSelectMatch, onClear, onSaved,
}: {
  generation: string
  freshness: string
  categories: string[]
  /** Currently loaded page(s) of rows — the only anchors whose current field
   * values are known locally; an "all matching" or cross-page selection can
   * include anchors outside this set. */
  rows: TransactionRowOut[]
  picked: Set<string>
  matchAll: Scope | null
  total: number
  filters: RawFilters
  onSelectPage: () => void
  onSelectMatch: (scope: Scope) => void
  onClear: () => void
  onSaved: (status: string) => void | Promise<void>
}) {
  const anchors = matchAll ? matchAll.anchors : [...picked]
  const known = anchors
    .map(a => rows.find(r => r.entry.anchor === a))
    .filter((r): r is TransactionRowOut => Boolean(r))
  const unknownCount = anchors.length - known.length

  const [category, setCategory] = useState('')
  const [costBehavior, setCostBehavior] = useState('')
  const [need, setNeed] = useState('')
  const [incomingType, setIncomingType] = useState('')
  const [clearCategory, setClearCategory] = useState(false)
  const [clearRoles, setClearRoles] = useState(false)
  const [policy, setPolicy] = useState<'fill_missing' | 'replace'>('fill_missing')
  const [reason, setReason] = useState('')
  const [preview, setPreview] = useState<ClassificationPreview | null>(null)
  const [previewError, setPreviewError] = useState('')
  const [previewing, setPreviewing] = useState(false)
  const [matching, setMatching] = useState(false)
  const [matchError, setMatchError] = useState('')

  const batch = useBatchCommand(generation, async status => { onClear(); await onSaved(status) })

  // A definitive 409/422 clears the frozen batch body; re-preview before
  // "Save changes" can resubmit against a selection that is now known stale.
  useEffect(() => { if (batch.error && !batch.submitted) setPreview(null) }, [batch.error, batch.submitted])

  // Any change to the frozen scope itself (a different page/all-matching
  // selection while this editor stays open) invalidates a standing preview,
  // same as a field/policy/draft change does via the form's onChange below.
  const scopeKey = matchAll ? `all:${matchAll.anchors.join(',')}` : `picked:${[...picked].sort().join(',')}`
  useEffect(() => { setPreview(null); setPreviewError(''); batch.invalidate() /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [scopeKey])

  const disabled = freshness === 'stale'
  const hasFieldEdit = Boolean(category || costBehavior || need || incomingType)
  const hasEdit = hasFieldEdit || clearCategory || clearRoles
  const reasonRequired = Boolean(costBehavior || need || incomingType || clearCategory || clearRoles)
  const dirty = hasEdit || reason !== ''

  function invalidatePreview() { setPreview(null); setPreviewError(''); batch.invalidate() }

  const allFieldsApplicable = { category: true, cost_behavior: true, need: true, incoming_type: true }
  const currentPresetFields: PresetFields = {
    ...(category ? { category } : {}), ...(costBehavior ? { cost_behavior: costBehavior } : {}),
    ...(need ? { need } : {}), ...(incomingType ? { incoming_type: incomingType } : {}),
  }
  function applyPreset(fields: PresetFields) {
    if (fields.category !== undefined) { setClearCategory(false); setCategory(fields.category) }
    if (fields.cost_behavior !== undefined) { setClearRoles(false); setCostBehavior(fields.cost_behavior) }
    if (fields.need !== undefined) { setClearRoles(false); setNeed(fields.need) }
    if (fields.incoming_type !== undefined) setIncomingType(fields.incoming_type)
    invalidatePreview()
  }

  function fieldValues(name: FieldName): string[] {
    return known
      .filter(r => applicability(detailBucket(r.entry))[name])
      .map(r => {
        if (name === 'category') return r.entry.category === UNCLASSIFIED ? '' : r.entry.category
        if (name === 'incoming_type') return r.entry.classification ?? ''
        const raw = name === 'cost_behavior' ? r.entry.cost_behavior : r.entry.need
        return raw !== 'N/A' ? raw : ''
      })
  }

  /** Best-effort context shown before any preview: a common value, "Multiple
   * values", or a note that the field doesn't apply — computed only from
   * anchors whose entry is on an already-loaded page. The preview below is
   * the authoritative, whole-selection answer once at least one field edit
   * is chosen (§3.3's "applies to N of M selected transactions"). */
  function fieldContext(name: FieldName): string {
    const values = fieldValues(name)
    const loadedNote = unknownCount > 0 ? ` — ${unknownCount} more selected not yet loaded` : ''
    if (!values.length) return `Not applicable to the ${known.length ? 'loaded' : ''} selection${loadedNote}`.replace('  ', ' ')
    const distinct = new Set(values)
    const display = distinct.size > 1
      ? 'Multiple values'
      : values[0] ? (name === 'category' ? shortCategory(values[0]) : label(name, values[0])) : 'Unresolved'
    return `${display} (${values.length} of ${known.length} loaded)${loadedNote}`
  }

  async function selectAllMatching() {
    setMatching(true); setMatchError('')
    try {
      const q = new URLSearchParams({ generation, history_start: filters.start, history_end: filters.end })
      if (filters.pocket_currency) q.set('pocket_currency', filters.pocket_currency)
      for (const [k, v] of Object.entries(filters)) {
        if (!['start', 'end', 'pocket_currency', 'include_excluded'].includes(k) && v) q.set(k, String(v))
      }
      if (filters.include_excluded) q.set('include_excluded', 'true')
      onSelectMatch(await api('reporting/transaction-anchors?' + q))
    } catch (e) { setMatchError((e as Error).message) }
    finally { setMatching(false) }
  }

  async function reviewChanges() {
    if (!hasEdit || previewing || !anchors.length || (reasonRequired && !reason.trim())) return
    setPreviewing(true); setPreviewError('')
    try {
      const draft: ClassificationDraftBody = {
        generation, anchors,
        category: category ? { intent: 'set', value: category } : undefined,
        cost_behavior: costBehavior ? { intent: 'set', value: costBehavior } : undefined,
        need: need ? { intent: 'set', value: need } : undefined,
        incoming_type: incomingType ? { intent: 'set', value: incomingType } : undefined,
        clear_category: clearCategory, clear_roles: clearRoles,
        policy, reason: reason.trim(),
      }
      setPreview(await previewClassification(draft))
      if (category) rememberCategory(category)
    } catch (err) { setPreviewError((err as Error).message) }
    finally { setPreviewing(false) }
  }

  async function saveChanges() {
    if (!preview) return
    await batch.submit(preview.commands, reason.trim())
  }

  const retrying = Boolean(batch.submitted && batch.error)

  return <div className="bulk-assign" tabIndex={-1} role="region" aria-label="Bulk assignment">
    <div className="bulk-head">
      <span><Layers3 size={15}/><strong>{anchors.length}</strong> selected{matchAll ? ' (all matching)' : ''}{matchAll?.truncated ? ' · capped: narrow the filter to select more' : ''}</span>
      {!matchAll && <Button variant="ghost" size="sm" onClick={onSelectPage}>Select this page</Button>}
      {!matchAll && picked.size > 0 && picked.size < total && <Button variant="ghost" size="sm" disabled={matching} onClick={() => void selectAllMatching()}>{matching ? 'Selecting…' : `Select all ${total} matching`}</Button>}
      <Button variant="ghost" size="sm" onClick={onClear}><X size={13}/>Clear</Button>
    </div>
    {matchAll && <p className="muted-note">Frozen scope: the filters in effect when you selected all matching ({filters.pocket_currency || 'all pockets'}, {filters.start} to {filters.end}{filters.search ? `, matching “${filters.search}”` : ''}{filters.kind ? `, type ${filters.kind}` : ''}). Changing filters now does not change what gets saved.</p>}
    {unknownCount > 0 && <p className="muted-note">{unknownCount} of {anchors.length} selected transactions are not on a currently loaded page. Their current values and applicability are shown exactly after Review changes.</p>}
    {matchError && <p className="form-error" role="alert">{matchError}</p>}

    <form onSubmit={event => { event.preventDefault(); if (!preview) void reviewChanges(); else void saveChanges() }} onChange={invalidatePreview}>
      <PresetPicker applicable={allFieldsApplicable} categories={categories} current={currentPresetFields}
        onApply={applyPreset} disabled={disabled || batch.busy}/>
      <ClassificationFieldsFieldset
        categoryId="bulk-category" categories={categories} disabled={disabled} busy={batch.busy}
        category={category} onCategory={setCategory} clearCategory={clearCategory} onClearCategory={setClearCategory}
        costBehavior={costBehavior} onCostBehavior={setCostBehavior} need={need} onNeed={setNeed}
        clearRoles={clearRoles} onClearRoles={setClearRoles}
        incomingType={incomingType} onIncomingType={setIncomingType}
        policy={policy} onPolicy={setPolicy}
        reason={reason} onReason={setReason} reasonRequired={reasonRequired}
        fieldContext={fieldContext}
      />
      {previewError && <p className="form-error" role="alert">{previewError}</p>}
      {batch.error && <p className="form-error" role="alert">{batch.error}</p>}
      {preview && <ClassificationPreviewSummary preview={preview}/>}
      {retrying && <p className="muted-note">The outcome may be unknown. Retry sends the same request safely; do not create a second decision.</p>}
      {disabled && <p className="form-error">Refresh the ledger before a bulk correction.</p>}
      <div className="dialog-actions">
        <Button type="submit" disabled={disabled || batch.busy || previewing || !anchors.length || (!preview && (!hasEdit || (reasonRequired && !reason.trim())))}>
          {previewing ? 'Checking…' : batch.busy ? 'Working…' : preview ? (retrying ? 'Retry same save' : `Save changes for ${preview.changed_transactions} transaction${preview.changed_transactions === 1 ? '' : 's'}`) : 'Review changes'}
        </Button>
      </div>
      {dirty && !preview && <p className="sr-only" aria-hidden="true">unsaved draft</p>}
    </form>
    {anchors.length > 0 && <RuleSection generation={generation} anchors={anchors}
      category={clearCategory ? '' : category} costBehavior={clearRoles ? '' : costBehavior} need={clearRoles ? '' : need}
      reason={reason} disabled={disabled} onSaved={onSaved}/>}
  </div>
}

