import { useCallback, useEffect, useRef, useState } from 'react'
import { Check, Link2, Pencil, RefreshCw, Wallet } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { AsyncView } from '@/components/AsyncView'
import { useLedgerCommand } from '@/lib/command'
import { adapterLabel } from '@/features/setup/api'
import { loadAccounts, type AccountRow, type AccountsPage, type PocketCoverage } from './api'
import { dateLabel } from '@/lib/format'

const STATUS_LABEL: Record<string, string> = { covered: 'Covered', gap: 'Gap', unconfigured: 'No expected window' }

/**
 * v0.5 slice 2 — account inventory grouped institution → account → pocket.
 * Coverage is per pocket: observed entry span vs the expected-active window set
 * with ACCOUNT_SCOPE_SET. Declaring an expected window never fabricates
 * transactions; archiving requires an end date and keeps history.
 */
export function AccountsView({ freshness, onSaved }: { freshness: string; onSaved: (status: string) => void | Promise<void> }) {
  const [page, setPage] = useState<AccountsPage | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const seq = useRef(0)

  const load = useCallback(() => {
    const id = ++seq.current
    setLoading(true); setError('')
    loadAccounts()
      .then(p => { if (id === seq.current) setPage(p) })
      .catch(e => { if (id === seq.current) setError((e as Error).message) })
      .finally(() => { if (id === seq.current) setLoading(false) })
  }, [])
  useEffect(() => { load() }, [load])

  const afterSave = async (status: string) => { await onSaved(status); load() }

  return (
    <AsyncView loading={loading} error={error} data={page} onRetry={load} loadingLabel="Loading accounts" errorTitle="We couldn’t load accounts.">
      {page => <AccountsContent page={page} freshness={freshness} loading={loading} load={load} afterSave={afterSave} />}
    </AsyncView>
  )
}

function AccountsContent({ page, freshness, loading, load, afterSave }: {
  page: AccountsPage; freshness: string; loading: boolean; load: () => void; afterSave: (status: string) => Promise<void>
}) {
  const covered = page.global_coverage?.covered
  const reasons = page.global_coverage?.reasons ?? []

  return (
    <div className="accounts-view">
      <div className="scope-caption">
        <span className={covered ? 'covered' : 'coverage-missing'}><span className="status-dot" />{covered ? 'All expected windows covered' : 'Coverage incomplete'}</span>
        <Button variant="ghost" size="sm" onClick={load} disabled={loading}><RefreshCw size={14} className={loading ? 'spin' : ''} /> Refresh</Button>
      </div>
      {reasons.length > 0 && (
        <div className="alert" role="status"><div><strong>Reports affected by current coverage</strong><ul>{reasons.map((r, i) => <li key={i}>{r}</li>)}</ul></div></div>
      )}
      {freshness === 'stale' && <div className="alert" role="status">New ledger inputs are available. Refresh before recording scope or label changes.</div>}

      {page.accounts.length === 0 && (
        <Card><div className="empty"><Wallet size={24} /><h3>No accounts yet</h3><p>Accounts appear after your first import. You can then declare their expected-active windows.</p></div></Card>
      )}

      {page.accounts.map(account => (
        <AccountCard key={account.id} account={account} generation={page.generation} disabled={freshness === 'stale'} onSaved={afterSave} />
      ))}

      <AliasSection page={page} disabled={freshness === 'stale'} onSaved={afterSave} />
    </div>
  )
}

function AccountCard({ account, generation, disabled, onSaved }: {
  account: AccountRow; generation: string; disabled: boolean; onSaved: (status: string) => void | Promise<void>
}) {
  const [editing, setEditing] = useState(false)
  return (
    <Card className="account-card">
      <div className="panel-title">
        <div>
          <p className="eyebrow">{adapterLabel(account.source_type)}</p>
          <h2>{account.display_name}{account.archived_on && <Badge variant="outline"> archived {dateLabel(account.archived_on)}</Badge>}</h2>
          <small className="muted-note">{account.masked_identifier || 'no masked identifier'} · {account.entry_count} entries</small>
        </div>
        <div className="account-status">
          <Badge variant="outline" className={account.coverage_status === 'gap' ? 'review-badge' : ''}>{STATUS_LABEL[account.coverage_status]}</Badge>
          <Button variant="ghost" size="sm" onClick={() => setEditing(v => !v)}><Pencil size={13} /> Label</Button>
        </div>
      </div>

      {editing && <DetailsForm account={account} generation={generation} disabled={disabled} onDone={() => setEditing(false)} onSaved={onSaved} />}

      <table className="pocket-table">
        <thead><tr><th>Pocket</th><th>Observed</th><th>Expected</th><th>Status</th><th><span className="sr-only">Set</span></th></tr></thead>
        <tbody>
          {account.pockets.map(pocket => (
            <PocketRow key={pocket.pocket} account={account} pocket={pocket} generation={generation} disabled={disabled} onSaved={onSaved} />
          ))}
          {account.pockets.length === 0 && <tr><td colSpan={5} className="muted-note">No entries recorded for this account yet.</td></tr>}
        </tbody>
      </table>
    </Card>
  )
}

function PocketRow({ account, pocket, generation, disabled, onSaved }: {
  account: AccountRow; pocket: PocketCoverage; generation: string; disabled: boolean; onSaved: (status: string) => void | Promise<void>
}) {
  const [open, setOpen] = useState(false)
  const [start, setStart] = useState(pocket.expected_start ?? '')
  const [end, setEnd] = useState(pocket.expected_end ?? '')
  const [reason, setReason] = useState('')
  const cmd = useLedgerCommand(generation, async s => { setOpen(false); await onSaved(s) })

  return (
    <>
      <tr>
        <td>{pocket.pocket}</td>
        <td>{pocket.observed_start ? `${pocket.observed_start} → ${pocket.observed_end}` : '—'}<small>{pocket.entry_count} entries</small></td>
        <td>{pocket.expected_start ? `${pocket.expected_start} → ${pocket.expected_end || 'open'}` : '—'}</td>
        <td><Badge variant="outline" className={pocket.coverage_status === 'gap' ? 'review-badge' : ''}>{STATUS_LABEL[pocket.coverage_status]}</Badge>{pocket.gaps.length > 0 && <small>No statement for {pocket.gaps.map(g => `${g.start} → ${g.end}`).join(', ')}</small>}</td>
        <td><Button variant="ghost" size="sm" onClick={() => { setOpen(v => !v); cmd.reset() }}>{open ? 'Cancel' : 'Set window'}</Button></td>
      </tr>
      {open && (
        <tr className="pocket-form-row"><td colSpan={5}>
          <form onChange={cmd.reset} onSubmit={e => { e.preventDefault(); void cmd.act({ kind: 'ACCOUNT_SCOPE_SET', anchors: { account: account.id, pocket: pocket.pocket }, payload: { expected_start: start, expected_end: end, reason } }) }}>
            <div className="form-row-3">
              <label>Expected start<Input required type="date" value={start} disabled={cmd.attempted} onChange={e => setStart(e.target.value)} /></label>
              <label>Expected end<Input type="date" value={end} disabled={cmd.attempted} onChange={e => setEnd(e.target.value)} /><small>Leave empty for an open-ended window.</small></label>
              <label>Reason<Input required maxLength={2000} value={reason} disabled={cmd.attempted} placeholder="Why this window?" onChange={e => setReason(e.target.value)} /></label>
            </div>
            {cmd.error && <p className="form-error" role="alert">{cmd.error}</p>}
            {cmd.previewed && <div className="preview" role="status"><Check size={15} /><p>Expected window for {account.display_name} · {pocket.pocket}: {start} → {end || 'open'}. No transactions are created.</p></div>}
            {disabled && <p className="form-error">Refresh the ledger before recording scope.</p>}
            <div className="dialog-actions"><Button type="submit" disabled={cmd.busy || disabled}>{cmd.busy ? 'Working…' : cmd.previewed ? (cmd.attempted ? 'Retry same save' : 'Save window') : 'Preview'}</Button></div>
          </form>
        </td></tr>
      )}
    </>
  )
}

function DetailsForm({ account, generation, disabled, onDone, onSaved }: {
  account: AccountRow; generation: string; disabled: boolean; onDone: () => void; onSaved: (status: string) => void | Promise<void>
}) {
  const [name, setName] = useState(account.display_name)
  const [archivedOn, setArchivedOn] = useState(account.archived_on)
  const [reason, setReason] = useState('')
  const cmd = useLedgerCommand(generation, async s => { onDone(); await onSaved(s) })
  return (
    <form className="account-details-form" onChange={cmd.reset} onSubmit={e => { e.preventDefault(); void cmd.act({ kind: 'ACCOUNT_DETAILS_SET', anchors: { account: account.id }, payload: { display_name: name.trim(), archived_on: archivedOn, reason } }) }}>
      <div className="form-row-3">
        <label>Display name<Input required maxLength={80} value={name} disabled={cmd.attempted} onChange={e => setName(e.target.value)} /></label>
        <label>Archived on<Input type="date" value={archivedOn} disabled={cmd.attempted} onChange={e => setArchivedOn(e.target.value)} /><small>Set a date to archive; clear to un-archive.</small></label>
        <label>Reason<Input required maxLength={2000} value={reason} disabled={cmd.attempted} onChange={e => setReason(e.target.value)} placeholder="Why the change?" /></label>
      </div>
      {cmd.error && <p className="form-error" role="alert">{cmd.error}</p>}
      {cmd.previewed && <div className="preview" role="status"><Check size={15} /><p>Rename to “{name.trim()}”{archivedOn ? `, archived ${archivedOn}` : ''}. Inventory identity is unchanged.</p></div>}
      {disabled && <p className="form-error">Refresh the ledger before editing labels.</p>}
      <div className="dialog-actions">
        <Button type="button" variant="ghost" size="sm" onClick={onDone}>Cancel</Button>
        <Button type="submit" disabled={cmd.busy || disabled}>{cmd.busy ? 'Working…' : cmd.previewed ? (cmd.attempted ? 'Retry same save' : 'Save label') : 'Preview'}</Button>
      </div>
    </form>
  )
}

function AliasSection({ page, disabled, onSaved }: { page: AccountsPage; disabled: boolean; onSaved: (status: string) => void | Promise<void> }) {
  const [sourceIdentity, setSourceIdentity] = useState('')
  const [accountId, setAccountId] = useState(page.accounts[0]?.id ?? '')
  const [reason, setReason] = useState('')
  const cmd = useLedgerCommand(page.generation, async s => { setSourceIdentity(''); setReason(''); await onSaved(s) })
  const retire = useLedgerCommand(page.generation, onSaved)

  return (
    <Card className="alias-section">
      <div className="panel-title"><div><p className="eyebrow">Source account aliases</p><h2>Bind a statement’s account identity to an owned account</h2></div></div>
      <p className="muted-note">Same bank name never merges two accounts. Binding is explicit and previewed for collisions.</p>
      {page.aliases.length > 0 && (
        <ul className="issue-list">
          {page.aliases.map(alias => (
            <li key={alias.source_identity}>
              <strong>{alias.source_identity}</strong> → {alias.account_id}
              <p>{alias.reason}</p>
              <Button variant="ghost" size="sm" disabled={disabled || retire.busy} onClick={() => void retire.act({ kind: 'ACCOUNT_ALIAS_RETIRE', anchors: { source_identity: alias.source_identity }, payload: { reason: `Unbind ${alias.source_identity}` } })}>Retire</Button>
            </li>
          ))}
        </ul>
      )}
      {retire.error && <p className="form-error" role="alert">{retire.error}</p>}
      <form onChange={cmd.reset} onSubmit={e => { e.preventDefault(); void cmd.act({ kind: 'ACCOUNT_ALIAS_SET', anchors: { source_identity: sourceIdentity.trim() }, payload: { account_id: accountId, reason } }) }}>
        <div className="form-row-3">
          <label>Source identity<Input required value={sourceIdentity} disabled={cmd.attempted} onChange={e => setSourceIdentity(e.target.value)} placeholder="bank:xxxx" /></label>
          <label>Owned account<select value={accountId} disabled={cmd.attempted} onChange={e => setAccountId(e.target.value)}>{page.accounts.map(a => <option key={a.id} value={a.id}>{a.display_name}</option>)}</select></label>
          <label>Reason<Input required maxLength={2000} value={reason} disabled={cmd.attempted} onChange={e => setReason(e.target.value)} /></label>
        </div>
        {cmd.error && <p className="form-error" role="alert">{cmd.error}</p>}
        {cmd.previewed && <div className="preview" role="status"><Check size={15} /><p><Link2 size={13} /> Bind {sourceIdentity.trim()} to {page.accounts.find(a => a.id === accountId)?.display_name}.</p></div>}
        {disabled && <p className="form-error">Refresh the ledger before binding an alias.</p>}
        <div className="dialog-actions"><Button type="submit" disabled={cmd.busy || disabled || !page.accounts.length}>{cmd.busy ? 'Working…' : cmd.previewed ? (cmd.attempted ? 'Retry same save' : 'Bind alias') : 'Preview'}</Button></div>
      </form>
    </Card>
  )
}
