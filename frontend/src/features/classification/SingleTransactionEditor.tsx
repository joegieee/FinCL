import { useEffect, useRef, useState } from 'react'
import { ArrowDownLeft, ArrowRight, Check } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { api, money, shortCategory, type Dashboard, type Entry } from '@/lib/api'
import { dateLabel } from '@/lib/format'
import { useBatchCommand } from '@/lib/command'
import { StatementDetailPanel } from '@/features/transactions'
import { OwnerShareForm } from '@/features/debts/OwnerShareForm'
import { EntryContextSection } from '@/features/trips/EntryContext'
import type { TransactionRowOut } from '@/features/transactions/reportedApi'
import { CategoryPicker, rememberCategory } from './CategoryPicker'
import { DebtHandoff, InstallmentHandoff, TransferHandoff, handoffTarget, movementDirection } from './Handoffs'
import { PresetPicker } from './PresetPicker'
import type { PresetFields } from './presets'
import { RuleSection } from './RuleSection'
import {
  COST_BEHAVIORS, NEEDS, INCOMING_TYPES, INCOMING_LABELS, applicability, detailBucket, label,
  previewClassification, type ClassificationDraftBody, type ClassificationPreview, type FieldName,
} from './api'

const UNCLASSIFIED = 'Unclassified / Unclassified'

/** What the movement *is*, asked before any classification field: ordinary
 * spending or money in is classified here; a transfer or a debt movement is
 * owned by a relationship decision instead, recorded through that workflow's
 * own form. */
type Nature = 'ordinary' | 'transfer' | 'debt'

const NATURES: Nature[] = ['ordinary', 'transfer', 'debt']

function natureLabel(nature: Nature, direction: 'IN' | 'OUT'): string {
  if (nature === 'ordinary') return direction === 'OUT' ? 'Spending' : 'Money in'
  if (nature === 'transfer') return 'Transfer between my accounts'
  return direction === 'OUT' ? 'Lending or repaying a debt' : 'Borrowing, or a debt repaid to me'
}

function natureOf(bucket: string): Nature {
  if (bucket === 'debt') return 'debt'
  if (bucket === 'internal_linked') return 'transfer'
  return 'ordinary'
}

/**
 * The unified single-transaction editor. Category, spending pattern, spending need and
 * (when applicable) incoming type are shown together and saved as one
 * reviewed batch through the Step 1 combined preview and retry-safe
 * lifecycle, replacing the old category/roles/inflow mode switch.
 */
export function EntryDialog({ entry, data, onClose, onSaved, onRelationshipSaved, statementDetail }: {
  entry: Entry
  data: Dashboard
  onClose: () => void
  onSaved: (status: string) => Promise<void>
  /** Refreshes the ledger after a relationship hand-off *without* closing this
   * dialog, so the editor can show what the decision made of the row. Falls
   * back to `onSaved` (which closes it) when not supplied. */
  onRelationshipSaved?: (status: string) => Promise<void>
  statementDetail?: TransactionRowOut
}) {
  // Held locally so a relationship decision recorded from inside the dialog
  // can replace it with the re-read row.
  const [e, setE] = useState(entry)
  const returnFocus = useRef<HTMLElement | null>(document.activeElement instanceof HTMLElement ? document.activeElement : null)
  useEffect(() => () => returnFocus.current?.focus(), [])
  // A statement-detail panel is only ever shown for the exact entry it was fetched for.
  const detail = statementDetail && statementDetail.entry.anchor === e.anchor ? statementDetail : undefined
  const bucket = detailBucket(e)
  const applicable = applicability(bucket)
  const direction = movementDirection(e)
  const [nature, setNature] = useState<Nature>(natureOf(bucket))
  const [installment, setInstallment] = useState(false)
  const [editingShare, setEditingShare] = useState(false)
  const [notice, setNotice] = useState('')

  const initialCategory = e.category === UNCLASSIFIED ? '' : e.category
  const initialCostBehavior = applicable.cost_behavior && e.cost_behavior !== 'N/A' ? e.cost_behavior : ''
  const initialNeed = applicable.need && e.need !== 'N/A' ? e.need : ''
  const initialIncomingType = applicable.incoming_type ? e.classification ?? '' : ''

  const [category, setCategory] = useState(initialCategory)
  const [costBehavior, setCostBehavior] = useState(initialCostBehavior)
  const [need, setNeed] = useState(initialNeed)
  const [incomingType, setIncomingType] = useState(initialIncomingType)
  const [clearCategory, setClearCategory] = useState(false)
  const [clearRoles, setClearRoles] = useState(false)
  const [reason, setReason] = useState('')
  const [preview, setPreview] = useState<ClassificationPreview | null>(null)
  const [previewError, setPreviewError] = useState('')
  const [previewing, setPreviewing] = useState(false)

  const batch = useBatchCommand(data.generation, async status => { await onSaved(status) })

  // A definitive 409/422 clears the frozen batch body; the classification
  // draft that produced it may now be stale too, so force a fresh preview
  // rather than letting "Save changes" resubmit commands nobody re-checked.
  useEffect(() => { if (batch.error && !batch.submitted) setPreview(null) }, [batch.error, batch.submitted])

  const categoryChanged = applicable.category && !clearCategory && category !== '' && category !== initialCategory
  const costBehaviorChanged = applicable.cost_behavior && !clearRoles && costBehavior !== '' && costBehavior !== initialCostBehavior
  const needChanged = applicable.need && !clearRoles && need !== '' && need !== initialNeed
  const incomingChanged = applicable.incoming_type && incomingType !== '' && incomingType !== initialIncomingType
  const hasEdit = categoryChanged || costBehaviorChanged || needChanged || incomingChanged || clearCategory || clearRoles
  const reasonRequired = costBehaviorChanged || needChanged || incomingChanged || clearCategory || clearRoles
  const dirty = hasEdit || reason !== ''

  function invalidatePreview() { setPreview(null); setPreviewError(''); batch.invalidate() }

  // "Current" for save-as-preset means fields the user has actively chosen —
  // i.e. already-changed relative to this entry's existing value — not
  // merely what the select happens to show, which starts pre-filled with the
  // entry's current value rather than a "keep existing" sentinel.
  const currentPresetFields: PresetFields = {
    ...(categoryChanged ? { category } : {}),
    ...(costBehaviorChanged ? { cost_behavior: costBehavior } : {}),
    ...(needChanged ? { need } : {}),
    ...(incomingChanged ? { incoming_type: incomingType } : {}),
  }
  function applyPreset(fields: PresetFields) {
    if (fields.category !== undefined) { setClearCategory(false); setCategory(fields.category) }
    if (fields.cost_behavior !== undefined) { setClearRoles(false); setCostBehavior(fields.cost_behavior) }
    if (fields.need !== undefined) { setClearRoles(false); setNeed(fields.need) }
    if (fields.incoming_type !== undefined) setIncomingType(fields.incoming_type)
    invalidatePreview()
  }

  const refreshKeepingOpen = onRelationshipSaved ?? onSaved

  /** After a hand-off records a decision: refresh, then re-read this row at
   * the new generation and say plainly what the decision made of it. */
  async function recorded(status: string, what: 'debt' | 'transfer' | 'installment' | 'owner_share') {
    await refreshKeepingOpen(status)
    try {
      const fresh = await api<{ generation: string; entry: Entry }>(`transactions/entry?anchor=${encodeURIComponent(e.anchor)}`)
      setE(fresh.entry)
      invalidatePreview()
      setInstallment(false)
      const freshBucket = detailBucket(fresh.entry)
      // A fully owned row gets the standing explanation below the notice, so
      // the notice only confirms the save rather than repeating it.
      if (what === 'owner_share') setNotice('Your expense share was updated. Debt principal is unchanged.')
      else if (freshBucket === 'debt') setNotice('Recorded against the debt.')
      else if (freshBucket === 'internal_linked') setNotice('Recorded as a transfer.')
      else if (what === 'debt') setNotice(fresh.entry.debt_allocation === 'partial'
        ? 'Recorded. Part of this amount now repays a personal debt; classify the rest below.'
        : 'Recorded.')
      else if (what === 'installment') setNotice('Linked to the installment plan. It still counts as spending, so keep its category below.')
      else setNotice('Recorded: these stay two ordinary transactions.')
      setNature(natureOf(freshBucket))
    } catch {
      setNotice('Recorded. Reopen this transaction to see its updated treatment.')
    }
  }

  function close() {
    if (batch.busy) return
    if (dirty && !confirm('Discard this unsaved draft?')) return
    onClose()
  }

  async function reviewChanges() {
    if (!hasEdit || previewing || (reasonRequired && !reason.trim())) return
    setPreviewing(true); setPreviewError('')
    try {
      const draft: ClassificationDraftBody = {
        generation: data.generation, anchors: [e.anchor],
        category: categoryChanged ? { intent: 'set', value: category } : undefined,
        cost_behavior: costBehaviorChanged ? { intent: 'set', value: costBehavior } : undefined,
        need: needChanged ? { intent: 'set', value: need } : undefined,
        incoming_type: incomingChanged ? { intent: 'set', value: incomingType } : undefined,
        clear_category: clearCategory, clear_roles: clearRoles,
        policy: 'replace', reason: reason.trim(),
      }
      setPreview(await previewClassification(draft))
      if (categoryChanged) rememberCategory(category)
    } catch (err) { setPreviewError((err as Error).message) }
    finally { setPreviewing(false) }
  }

  async function saveChanges() {
    if (!preview) return
    await batch.submit(preview.commands, reason.trim())
  }

  const disabled = data.freshness === 'stale'
  const notEditable = bucket === 'excluded' || bucket === 'internal_linked' || bucket === 'debt' || bucket === 'installment'
  const retrying = Boolean(batch.submitted && batch.error)

  return <Dialog open onOpenChange={open => { if (!open) close() }}>
    <DialogContent className="detail-dialog">
      <DialogHeader><DialogTitle>{e.merchant}</DialogTitle><DialogDescription>Original transaction and recorded decisions</DialogDescription></DialogHeader>
      <div className="detail-amount">{money(e.amount, e.currency)}</div>
      <dl className="detail-list">
        <div><dt>Date</dt><dd>{e.precision === 'MONTH' ? e.date.slice(0, 7) + ' · month precision' : dateLabel(e.date)}</dd></div>
        <div><dt>Account</dt><dd>{e.account}</dd></div>
        <div><dt>Fee</dt><dd>{money(e.fee, e.currency)}</dd></div>
        <div><dt>Cash-flow treatment</dt><dd>{e.bucket === 'debt' ? 'personal debt' : e.bucket === 'installment' ? 'installment plan' : e.bucket.replaceAll('_', ' ')}{e.debt_allocation === 'partial' && <small> · part of this amount is allocated to a personal debt; the classification below covers the rest</small>}</dd></div>
        <div><dt>Category basis</dt><dd>{e.basis}</dd></div>
        {applicable.cost_behavior && <>
          <div><dt>Spending pattern basis</dt><dd>{e.cost_behavior_basis}</dd></div>
          <div><dt>Need basis</dt><dd>{e.need_basis}</dd></div>
        </>}
      </dl>
      <div className="source-description">
        <span className="eyebrow">ORIGINAL DESCRIPTION</span><p>{e.description}</p>
        <small>{e.source_family} · {e.lifecycle_state} · {e.source_location}</small>
        <a className="source-download" href={`/api/v1/evidence/${encodeURIComponent(e.id)}?generation=${encodeURIComponent(data.generation)}`} download>
          Download verified source statement <ArrowDownLeft size={14}/>
        </a>
      </div>
      {detail && <StatementDetailPanel row={detail}/>}

      <h3>Update this transaction</h3>
      {notice && <p className="muted-note" role="status">{notice}</p>}
      {direction === 'OUT' && e.debt_allocation && !['excluded', 'internal_linked', 'installment'].includes(bucket) && (
        editingShare ? <OwnerShareForm key={`${e.anchor}-${data.generation}`} anchor={e.anchor} generation={data.generation}
          categories={data.categories} onCancel={() => setEditingShare(false)}
          onSaved={async status => { await recorded(status, 'owner_share'); setEditingShare(false) }}/>
          : <Button type="button" size="sm" variant="outline" disabled={disabled || batch.busy}
              onClick={() => setEditingShare(true)}>Review my expense share</Button>
      )}
      {notEditable
        ? <p className="muted-note">
            {bucket === 'excluded'
              ? 'This transaction is not active in the current selection scope and cannot be classified.'
              : bucket === 'debt'
                ? 'This movement is fully explained by debt allocations and any recorded expense share. Principal is neither spending nor income; your expense share keeps its own category. Review your share here. To change what was recorded against the debt, open the debt on the Debts page and correct or remove the event in its history.'
                : bucket === 'installment'
                  ? 'This movement belongs to a confirmed installment plan, so it is neither spending nor income: the original purchase carries the spending and its category. Change the plan on the Installments page instead of classifying it here.'
                  : 'The linked relationship owns this movement; change the transfer decision instead of classifying it here.'}
          </p>
        : <div className="nature-choice" role="radiogroup" aria-label="What is this?">
            <span className="eyebrow">WHAT IS THIS?</span>
            {NATURES.map(option => <button key={option} type="button" role="radio" aria-checked={nature === option}
              className={nature === option ? 'is-active' : ''} disabled={batch.busy}
              onClick={() => { setNature(option); setInstallment(false); setNotice('') }}>
              {natureLabel(option, direction)}
            </button>)}
          </div>}
      {!notEditable && nature === 'transfer' && (disabled
        ? <p className="form-error">Refresh the ledger before editing.</p>
        : <TransferHandoff entry={e} data={data} onRecorded={status => recorded(status, 'transfer')}
            onFromOwnAccount={applicable.incoming_type ? () => { setIncomingType('self_transfer'); invalidatePreview(); setNature('ordinary'); setNotice('Incoming type set to self-transfer below; review and save it.') } : undefined}/>)}
      {!notEditable && nature === 'debt' && (disabled
        ? <p className="form-error">Refresh the ledger before editing.</p>
        : <DebtHandoff entry={handoffTarget(e)} generation={data.generation} onRecorded={status => recorded(status, 'debt')} onRefresh={refreshKeepingOpen}/>)}
      {!notEditable && nature === 'ordinary' && <>
      <form onSubmit={event => { event.preventDefault(); if (!preview) void reviewChanges(); else void saveChanges() }} onChange={invalidatePreview}>
            <PresetPicker applicable={applicable} categories={data.categories} current={currentPresetFields}
              onApply={applyPreset} disabled={disabled || batch.busy}/>
            {applicable.category && <>
              <CategoryPicker id={`category-${e.anchor}`} categories={data.categories} value={clearCategory ? '' : category}
                onChange={c => { setClearCategory(false); setCategory(c) }} disabled={disabled || batch.busy || clearCategory}/>
              {e.basis === 'override' && <label>
                <input type="checkbox" checked={clearCategory} disabled={disabled || batch.busy}
                  onChange={event => { setClearCategory(event.target.checked); if (event.target.checked) setCategory(initialCategory) }}/>
                {' '}Remove manual category override
              </label>}
            </>}
            {applicable.category && applicable.incoming_type &&
              <p className="muted-note">Category describes what the money was for; incoming type decides whether it counts as income.</p>}
            {applicable.cost_behavior && <>
              <label>Spending pattern
                <select value={clearRoles ? '' : costBehavior} disabled={disabled || batch.busy || clearRoles} onChange={event => setCostBehavior(event.target.value)}>
                  <option value="">Keep current pattern</option>
                  {COST_BEHAVIORS.map(v => <option key={v} value={v}>{label('cost_behavior', v)}</option>)}
                </select>
              </label>
              <label>Spending need
                <select value={clearRoles ? '' : need} disabled={disabled || batch.busy || clearRoles} onChange={event => setNeed(event.target.value)}>
                  <option value="">Keep current need</option>
                  {NEEDS.map(v => <option key={v} value={v}>{label('need', v)}</option>)}
                </select>
              </label>
              {(e.cost_behavior_basis === 'manual' || e.need_basis === 'manual') && <label>
                <input type="checkbox" checked={clearRoles} disabled={disabled || batch.busy}
                  onChange={event => { setClearRoles(event.target.checked); if (event.target.checked) { setCostBehavior(initialCostBehavior); setNeed(initialNeed) } }}/>
                {' '}Remove manual spending classifications
                <small> Both pattern and need return to their category-derived defaults.</small>
              </label>}
            </>}
            {applicable.incoming_type && <label>Incoming type
              <select value={incomingType} disabled={disabled || batch.busy} onChange={event => setIncomingType(event.target.value)}>
                <option value="">{initialIncomingType ? 'Keep current type' : 'Choose incoming type'}</option>
                {INCOMING_TYPES.map(v => <option key={v} value={v}>{INCOMING_LABELS[v]}</option>)}
              </select>
            </label>}
            <label>Reason {!reasonRequired && <small>(optional)</small>}
              <Input maxLength={2000} disabled={disabled || batch.busy} required={reasonRequired}
                placeholder={reasonRequired ? 'Why is this the right classification?' : 'Optional note for your history'}
                value={reason} onChange={event => setReason(event.target.value)}/>
            </label>
        {previewError && <p className="form-error" role="alert">{previewError}</p>}
        {batch.error && <p className="form-error" role="alert">{batch.error}</p>}
        {preview && <PreviewSummary preview={preview} entryAnchor={e.anchor}/>}
        {retrying && <p className="muted-note">The outcome may be unknown. Retry sends the same request safely; do not create a second decision.</p>}
        {disabled && <p className="form-error">Refresh the ledger before editing.</p>}
        <div className="dialog-actions">
          <Button type="submit" disabled={disabled || batch.busy || previewing || (!preview && (!hasEdit || (reasonRequired && !reason.trim())))}>
            {previewing ? 'Checking…' : batch.busy ? 'Working…' : preview ? (retrying ? 'Retry same save' : 'Save changes') : 'Review changes'}
            {!previewing && !batch.busy && <ArrowRight size={15}/>}
          </Button>
        </div>
      </form>
      {/* An installment plan splits a purchase that is still spending, so this
          is an extra link alongside the classification above, not an
          alternative to it. */}
      {bucket === 'spending' && !disabled && (installment
        ? <InstallmentHandoff entry={handoffTarget(e)} generation={data.generation} onRecorded={status => recorded(status, 'installment')} onCancel={() => setInstallment(false)}/>
        : <Button type="button" variant="link" size="sm" onClick={() => setInstallment(true)}>Part of an installment plan?</Button>)}
      <RuleSection generation={data.generation} anchors={[e.anchor]}
        category={categoryChanged ? category : ''} costBehavior={costBehaviorChanged ? costBehavior : ''} need={needChanged ? need : ''}
        reason={reason} disabled={disabled || batch.busy} onSaved={onSaved}/>
      </>}
      {bucket !== 'excluded' && <EntryContextSection anchor={e.anchor} generation={data.generation} disabled={disabled} onSaved={refreshKeepingOpen}/>}
    </DialogContent>
  </Dialog>
}

function PreviewSummary({ preview, entryAnchor }: { preview: ClassificationPreview; entryAnchor: string }) {
  const row = preview.rows.find(r => r.anchor === entryAnchor)
  if (!row) return null
  const changes = (Object.keys(row.fields) as FieldName[]).map(name => [name, row.fields[name]] as const).filter(([, outcome]) => outcome.changed)
  if (!changes.length) return <div className="preview" role="status"><Check size={16}/><div><strong>No changes to save</strong><p>Every selected field already matches this decision.</p></div></div>
  return <div className="preview" role="status"><Check size={16}/><div>
    <strong>Ready to save</strong>
    {changes.map(([field, outcome]) => <p key={field}>
      {FIELD_LABELS[field]}: {fieldValue(field, outcome.before)} → {fieldValue(field, outcome.after)}
      {outcome.derived ? ' (from category default)' : ''}
      {outcome.manual_replacement ? ' — replaces a manual value' : ''}
    </p>)}
  </div></div>
}

const FIELD_LABELS: Record<FieldName, string> = {
  category: 'Category', cost_behavior: 'Spending pattern', need: 'Spending need', incoming_type: 'Incoming type',
}

function fieldValue(field: FieldName, value: string | null): string {
  if (value == null) return 'None'
  return field === 'category' ? shortCategory(value) : label(field, value)
}
