import { useState } from 'react'
import { Bookmark, Pencil, Plus, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { shortCategory } from '@/lib/api'
import { label as fieldLabel } from './api'
import { useAssignableCategories } from './CategoryPicker'
import {
  deletePreset, loadPresets, renamePreset, resolvePreset, savePreset,
  type FieldApplicability, type Preset, type PresetFields,
} from './presets'

function summarize(fields: PresetFields): string {
  const parts: string[] = []
  if (fields.category) parts.push(shortCategory(fields.category))
  if (fields.cost_behavior) parts.push(fieldLabel('cost_behavior', fields.cost_behavior))
  if (fields.need) parts.push(fieldLabel('need', fields.need))
  if (fields.incoming_type) parts.push(fieldLabel('incoming_type', fields.incoming_type))
  return parts.join(' · ')
}

/**
 * Named, browser-local classification presets (PLAN-unified-transaction-
 * classification.md Step 6). Applying a preset only fills the caller's own
 * draft fields via `onApply`; it never saves, never touches the bulk
 * fill-missing/replace policy, and stores no reason or transaction data.
 * Mounted once per editor (single-transaction, bulk, grouped review) so all
 * three share this one implementation rather than three copies of it.
 */
export function PresetPicker({ applicable, categories, current, onApply, disabled }: {
  applicable: FieldApplicability
  /** The raw category list (same as passed to the category picker); this
   * component derives the assignable (non-archived, non-internal) subset
   * itself, and only fetches that metadata while actually mounted — i.e.
   * only for a transaction/selection that is actually editable. */
  categories: string[]
  /** The field values presently chosen in the caller's own draft, restricted
   * to fields that are both applicable and non-empty; this is exactly what
   * "Save as preset" offers to store. */
  current: PresetFields
  /** Receives only the applicable, still-valid fields from the chosen
   * preset. The caller applies them to its own state and invalidates any
   * standing preview — applying a preset is a form edit like any other. */
  onApply: (fields: PresetFields) => void
  disabled?: boolean
}) {
  const assignableCategories = useAssignableCategories(categories)
  const [presets, setPresets] = useState<Preset[]>(() => loadPresets())
  const [name, setName] = useState('')
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [renameValue, setRenameValue] = useState('')
  const [notice, setNotice] = useState('')

  const hasCurrent = Object.keys(current).length > 0

  function apply(preset: Preset) {
    const { fields, staleCategory } = resolvePreset(preset, applicable, assignableCategories)
    setNotice(staleCategory ? `"${preset.name}"'s saved category is archived or no longer exists — its other fields were applied.` : '')
    if (Object.keys(fields).length) onApply(fields)
  }

  function saveCurrentAsPreset() {
    const trimmed = name.trim()
    if (!trimmed || !hasCurrent) return
    setPresets(savePreset(trimmed, current))
    setName('')
  }

  function startRename(preset: Preset) { setRenamingId(preset.id); setRenameValue(preset.name) }

  function commitRename() {
    if (!renamingId) return
    const trimmed = renameValue.trim()
    if (trimmed) setPresets(renamePreset(renamingId, trimmed))
    setRenamingId(null); setRenameValue('')
  }

  function remove(preset: Preset) {
    if (!confirm(`Delete the "${preset.name}" preset? This only removes the saved preset, not any transaction it was previously applied to.`)) return
    setPresets(deletePreset(preset.id))
    if (renamingId === preset.id) { setRenamingId(null); setRenameValue('') }
  }

  return <div className="preset-picker">
    <div className="preset-picker-head"><Bookmark size={13}/><strong>Presets</strong><small>Saved in this browser · fills fields below, does not save</small></div>
    {!presets.length && <p className="muted-note">No presets saved yet. Choose field values below, then save them as a preset.</p>}
    {presets.length > 0 && <ul className="preset-list">
      {presets.map(preset => <li key={preset.id}>
        {renamingId === preset.id
          ? <>
              <Input aria-label={`Rename ${preset.name}`} value={renameValue} maxLength={80}
                onChange={e => setRenameValue(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); commitRename() } else if (e.key === 'Escape') { setRenamingId(null); setRenameValue('') } }}/>
              <Button type="button" size="sm" onClick={commitRename}>Save name</Button>
              <Button type="button" variant="ghost" size="sm" onClick={() => { setRenamingId(null); setRenameValue('') }}>Cancel</Button>
            </>
          : <>
              <Button type="button" variant="outline" size="sm" disabled={disabled} onClick={() => apply(preset)}>{preset.name}</Button>
              <small className="preset-summary">{summarize(preset.fields)}</small>
              <Button type="button" variant="ghost" size="sm" aria-label={`Rename ${preset.name}`} disabled={disabled} onClick={() => startRename(preset)}><Pencil size={12}/></Button>
              <Button type="button" variant="ghost" size="sm" aria-label={`Delete ${preset.name}`} disabled={disabled} onClick={() => remove(preset)}><Trash2 size={12}/></Button>
            </>}
      </li>)}
    </ul>}
    {notice && <p className="muted-note" role="status">{notice}</p>}
    <div className="preset-save-row">
      <Input aria-label="New preset name" placeholder={hasCurrent ? 'Name this selection, e.g. Everyday groceries' : 'Choose field values above first'}
        value={name} maxLength={80} disabled={disabled || !hasCurrent} onChange={e => setName(e.target.value)}/>
      <Button type="button" variant="outline" size="sm" disabled={disabled || !hasCurrent || !name.trim()} onClick={saveCurrentAsPreset}>
        <Plus size={12}/>Save as preset
      </Button>
    </div>
  </div>
}
