import { Input } from '@/components/ui/input'
import { CategoryPicker } from './CategoryPicker'
import { COST_BEHAVIORS, NEEDS, INCOMING_TYPES, INCOMING_LABELS, label, type FieldName } from './api'

/**
 * The shared category/spending-pattern/spending-need/incoming-type fields,
 * extracted from `BulkEditor` (Step 3) so grouped review (Step 5) can offer
 * the same grocery trio without a second copy of this markup
 * (docs/PLAN-unified-transaction-classification.md §4: "Use one shared
 * draft/preview/save implementation ... do not copy a separate editor per
 * page"). Purely controlled: the caller owns every value and decides which
 * fields/policy to show.
 */
export function ClassificationFieldsFieldset({
  categoryId, categories, disabled, busy,
  category, onCategory, clearCategory, onClearCategory,
  costBehavior, onCostBehavior, need, onNeed, clearRoles, onClearRoles,
  incomingType, onIncomingType,
  policy, onPolicy, showPolicy = true,
  reason, onReason, reasonRequired,
  fieldContext, hideCategory = false,
}: {
  categoryId: string
  categories: string[]
  disabled?: boolean
  busy?: boolean
  category: string
  onCategory: (value: string) => void
  clearCategory: boolean
  onClearCategory: (checked: boolean) => void
  costBehavior: string
  onCostBehavior: (value: string) => void
  need: string
  onNeed: (value: string) => void
  clearRoles: boolean
  onClearRoles: (checked: boolean) => void
  incomingType: string
  onIncomingType: (value: string) => void
  policy: 'fill_missing' | 'replace'
  onPolicy: (value: 'fill_missing' | 'replace') => void
  showPolicy?: boolean
  reason: string
  onReason: (value: string) => void
  reasonRequired: boolean
  fieldContext?: (name: FieldName) => string
  /** Skip the built-in category picker when the caller renders its own (e.g.
   * grouped review's inline-category-creation combobox), while still sharing
   * the trio/incoming/policy/reason fields below. */
  hideCategory?: boolean
}) {
  return <div className="bulk-form">
    {!hideCategory && <div className="bulk-field">
      <CategoryPicker id={categoryId} categories={categories} value={clearCategory ? '' : category}
        onChange={c => { onClearCategory(false); onCategory(c) }} disabled={disabled || busy || clearCategory}/>
      {fieldContext && <small className="field-context">Current: {fieldContext('category')}</small>}
      <label><input type="checkbox" checked={clearCategory} disabled={disabled || busy}
        onChange={event => { onClearCategory(event.target.checked); if (event.target.checked) onCategory('') }}/>
        {' '}Remove manual category overrides <small>(only affects transactions that have one)</small>
      </label>
    </div>}
    <div className="bulk-field">
      <label>Spending pattern
        <select aria-label="Set spending pattern" value={clearRoles ? '' : costBehavior} disabled={disabled || busy || clearRoles} onChange={e => onCostBehavior(e.target.value)}>
          <option value="">Keep existing values</option>
          {COST_BEHAVIORS.map(v => <option key={v} value={v}>{label('cost_behavior', v)}</option>)}
        </select>
      </label>
      {fieldContext && <small className="field-context">Current: {fieldContext('cost_behavior')}</small>}
    </div>
    <div className="bulk-field">
      <label>Spending need
        <select aria-label="Set spending need" value={clearRoles ? '' : need} disabled={disabled || busy || clearRoles} onChange={e => onNeed(e.target.value)}>
          <option value="">Keep existing values</option>
          {NEEDS.map(v => <option key={v} value={v}>{label('need', v)}</option>)}
        </select>
      </label>
      {fieldContext && <small className="field-context">Current: {fieldContext('need')}</small>}
      <label><input type="checkbox" checked={clearRoles} disabled={disabled || busy}
        onChange={event => { onClearRoles(event.target.checked); if (event.target.checked) { onCostBehavior(''); onNeed('') } }}/>
        {' '}Remove manual spending classifications <small>(only affects transactions that have one; both pattern and need return to category defaults)</small>
      </label>
    </div>
    <div className="bulk-field">
      <label>Incoming type
        <select aria-label="Set incoming type" value={incomingType} disabled={disabled || busy} onChange={e => onIncomingType(e.target.value)}>
          <option value="">Keep existing values</option>
          {INCOMING_TYPES.map(v => <option key={v} value={v}>{INCOMING_LABELS[v]}</option>)}
        </select>
      </label>
      {fieldContext && <small className="field-context">Current: {fieldContext('incoming_type')}</small>}
    </div>
    {showPolicy && <fieldset className="bulk-policy">
      <legend>When a chosen field already has a value</legend>
      <label><input type="radio" name="classification-policy" checked={policy === 'fill_missing'} disabled={disabled || busy} onChange={() => onPolicy('fill_missing')}/> Fill missing fields only <small>(never overwrites an existing value)</small></label>
      <label><input type="radio" name="classification-policy" checked={policy === 'replace'} disabled={disabled || busy} onChange={() => onPolicy('replace')}/> Replace selected fields <small>(overwrites existing values, including manual ones)</small></label>
    </fieldset>}
    <label>Reason {!reasonRequired && <small>(optional)</small>}
      <Input maxLength={2000} disabled={disabled || busy} required={reasonRequired}
        placeholder={reasonRequired ? 'Why is this the right classification?' : 'Optional note for your history'}
        value={reason} onChange={e => onReason(e.target.value)}/>
    </label>
  </div>
}
