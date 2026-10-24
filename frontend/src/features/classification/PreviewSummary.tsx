import { reportedMoney, WorkspaceReportingDetails } from '@/lib/workspaceReporting'
import { Check } from 'lucide-react'
import type { ClassificationPreview, FieldName } from './api'

export const FIELD_LABELS: Record<FieldName, string> = {
  category: 'Category', cost_behavior: 'Spending pattern', need: 'Spending need', incoming_type: 'Incoming type',
}

/**
 * Shared multi-anchor preview summary: every entry point that previews a
 * `ClassificationDraftBody` over more than one anchor -- bulk selection,
 * income receipt confirmation -- renders the exact same combined-impact
 * disclosure, computed once by `evaluate_classification_draft` and never
 * re-derived client-side.
 */
export function ClassificationPreviewSummary({ preview }: { preview: ClassificationPreview }) {
  if (!preview.command_count) {
    return <div className="preview" role="status"><Check size={16}/><div>
      <strong>No changes to save</strong>
      <p>Every applicable transaction in this selection already matches this decision.</p>
    </div></div>
  }
  const activeFields = (Object.keys(preview.fields) as FieldName[]).filter(name => preview.fields[name].applicable > 0)
  return <div className="preview" role="status"><Check size={16}/><div>
    <WorkspaceReportingDetails data={preview}/><strong>Ready to save {preview.changed_transactions} transaction{preview.changed_transactions === 1 ? '' : 's'} ({preview.command_count} decision{preview.command_count === 1 ? '' : 's'})</strong>
    {preview.by_currency.map(row => <p key={row.currency}>
      {row.currency} · {row.count} selected, {row.changed} changing, {reportedMoney(row, 'absolute_amount', row.currency)} total
    </p>)}
    {activeFields.map(name => {
      const f = preview.fields[name]
      return <p key={name} className="muted-note">
        {FIELD_LABELS[name]} applies to {f.applicable} of {f.applicable + f.not_applicable} selected
        {f.changed ? `, ${f.changed} changing` : ''}
        {f.skipped_fill_missing ? `, ${f.skipped_fill_missing} already set and left unchanged` : ''}
        {f.manual_replacements ? `, ${f.manual_replacements} replacing a manual value` : ''}
        {f.derived ? `, ${f.derived} changing only as a category-derived effect` : ''}.
      </p>
    })}
  </div></div>
}
