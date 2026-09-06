import { useCallback, useEffect, useRef, useState } from 'react'
import { Archive, ArchiveRestore, Check, Pencil } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { api } from '@/lib/api'
import { useLedgerCommand } from '@/lib/command'

type CustomCategory = { key: string; display_group: string; display_name: string; archived: boolean; custom: boolean }
type CategoriesPage = { generation: string; categories: CustomCategory[] }

/**
 * v0.5 slice 3 — display metadata and archive state for user-created
 * categories. The Group / Name key is immutable; only the shown group/name and
 * the archived flag change. Archive hides a category from new assignments, not
 * from existing decisions or reports.
 */
export function CustomCategoryManager({ freshness, onSaved }: {
  freshness: string; onSaved: (status: string) => void | Promise<void>
}) {
  const [page, setPage] = useState<CategoriesPage | null>(null)
  const [error, setError] = useState('')
  const seq = useRef(0)

  const load = useCallback(() => {
    const id = ++seq.current
    setError('')
    api<CategoriesPage>('categories')
      .then(p => { if (id === seq.current) setPage(p) })
      .catch(e => { if (id === seq.current) setError((e as Error).message) })
  }, [])
  useEffect(() => { load() }, [load])

  const afterSave = async (status: string) => { await onSaved(status); load() }
  const disabled = freshness === 'stale'

  return (
    <Card className="custom-category-manager">
      <div className="panel-title"><div><p className="eyebrow">CUSTOM CATEGORIES</p><h2>Rename or archive categories you created</h2></div></div>
      {error && <div className="alert error" role="alert"><p>{error}</p></div>}
      {page && page.categories.length === 0 && <p className="muted-note">You have not created any custom categories yet. Use “New category” above.</p>}
      {page?.categories.map(category => (
        <CustomCategoryRow key={category.key} category={category} generation={page.generation} disabled={disabled} onSaved={afterSave} />
      ))}
      {disabled && <p className="form-error">Refresh the ledger before editing categories.</p>}
    </Card>
  )
}

function CustomCategoryRow({ category, generation, disabled, onSaved }: {
  category: CustomCategory; generation: string; disabled: boolean; onSaved: (status: string) => void | Promise<void>
}) {
  const [mode, setMode] = useState<'idle' | 'rename' | 'archive'>('idle')
  const [group, setGroup] = useState(category.display_group)
  const [name, setName] = useState(category.display_name)
  const [reason, setReason] = useState('')
  const cmd = useLedgerCommand(generation, async s => { setMode('idle'); setReason(''); await onSaved(s) })

  return (
    <div className="custom-category-row">
      <div>
        <strong>{category.display_group} / {category.display_name}</strong>
        {category.archived && <Badge variant="outline"> archived</Badge>}
        <small className="mono">{category.key}</small>
      </div>
      {mode === 'idle' && (
        <div className="row-actions">
          <Button variant="ghost" size="sm" disabled={disabled} onClick={() => { setMode('rename'); cmd.reset() }}><Pencil size={13} /> Rename</Button>
          <Button variant="ghost" size="sm" disabled={disabled} onClick={() => { setMode('archive'); cmd.reset() }}>
            {category.archived ? <><ArchiveRestore size={13} /> Restore</> : <><Archive size={13} /> Archive</>}
          </Button>
        </div>
      )}

      {mode === 'rename' && (
        <form onChange={cmd.reset} onSubmit={e => { e.preventDefault(); void cmd.act({ kind: 'CATEGORY_DETAILS_SET', anchors: { category: category.key }, payload: { display_group: group.trim(), display_name: name.trim(), reason } }) }}>
          <div className="form-row-3">
            <label>Group<Input required maxLength={80} value={group} disabled={cmd.attempted} onChange={e => setGroup(e.target.value)} /></label>
            <label>Name<Input required maxLength={80} value={name} disabled={cmd.attempted} onChange={e => setName(e.target.value)} /></label>
            <label>Reason<Input required maxLength={2000} value={reason} disabled={cmd.attempted} onChange={e => setReason(e.target.value)} /></label>
          </div>
          {cmd.error && <p className="form-error" role="alert">{cmd.error}</p>}
          {cmd.previewed && <div className="preview" role="status"><Check size={15} /><p>Shown as “{group.trim()} / {name.trim()}”. Rules and past decisions keep the original key.</p></div>}
          <div className="dialog-actions">
            <Button type="button" variant="ghost" size="sm" onClick={() => setMode('idle')}>Cancel</Button>
            <Button type="submit" disabled={cmd.busy}>{cmd.busy ? 'Working…' : cmd.previewed ? (cmd.attempted ? 'Retry same save' : 'Save name') : 'Preview'}</Button>
          </div>
        </form>
      )}

      {mode === 'archive' && (
        <form onChange={cmd.reset} onSubmit={e => { e.preventDefault(); void cmd.act({ kind: 'CATEGORY_ARCHIVE_SET', anchors: { category: category.key }, payload: { archived: !category.archived, reason } }) }}>
          <label>Reason<Input required maxLength={2000} value={reason} disabled={cmd.attempted} onChange={e => setReason(e.target.value)} placeholder={category.archived ? 'Why restore it?' : 'Why archive it?'} /></label>
          {cmd.error && <p className="form-error" role="alert">{cmd.error}</p>}
          {cmd.previewed && <div className="preview" role="status"><Check size={15} /><p>{category.archived ? 'Restores this category for new assignments.' : 'Hides this category from new assignments. Existing decisions and reports are unaffected.'}</p></div>}
          <div className="dialog-actions">
            <Button type="button" variant="ghost" size="sm" onClick={() => setMode('idle')}>Cancel</Button>
            <Button type="submit" disabled={cmd.busy}>{cmd.busy ? 'Working…' : cmd.previewed ? (cmd.attempted ? 'Retry same save' : category.archived ? 'Restore' : 'Archive') : 'Preview'}</Button>
          </div>
        </form>
      )}
    </div>
  )
}
