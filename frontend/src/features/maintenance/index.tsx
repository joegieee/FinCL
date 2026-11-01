import { useEffect, useState } from 'react'
import { Archive, Check, RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { api } from '@/lib/api'

type BackupResult = { backup_id: string; journal_seq?: number; evidence_objects: number; verified: boolean; generation?: string }

export function MaintenanceView() {
  const [backups, setBackups] = useState<string[]>([]), [selected, setSelected] = useState(''), [result, setResult] = useState<BackupResult | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState(false)
  function load() { api<{ backups: string[] }>('maintenance/backups').then(r => { setBackups(r.backups); if (!selected && r.backups[0]) setSelected(r.backups[0]) }).catch(e => setError((e as Error).message)) }
  useEffect(load, [])
  async function backup() { setBusy(true); setError(''); try { const r = await api<BackupResult>('maintenance/backup', {}); setResult(r); setSelected(r.backup_id); load() } catch (e) { setError((e as Error).message) } finally { setBusy(false) } }
  async function restore() { setBusy(true); setError(''); try { setResult(await api<BackupResult>('maintenance/restore-verify', { backup_id: selected })) } catch (e) { setError((e as Error).message) } finally { setBusy(false) } }
  return <div className="maintenance-view"><Card className="controls-card"><div className="panel-title"><div><p className="eyebrow">Backup &amp; restore</p><h2>Verify a complete recoverable ledger</h2></div><Archive size={19}/></div><p className="muted-note">Each backup captures the database, decision history, identity and every inventoried evidence object. Restore verification uses a separate temporary destination and never overwrites this ledger.</p>{error && <div className="alert error" role="alert">{error}</div>}<div className="row-actions"><Button onClick={() => void backup()} disabled={busy}>{busy ? 'Working…' : 'Create verified backup'}</Button><select aria-label="Backup to restore" value={selected} onChange={e => setSelected(e.target.value)}><option value="">Choose backup</option>{backups.map(b => <option key={b}>{b}</option>)}</select><Button variant="outline" onClick={() => void restore()} disabled={busy || !selected}><RefreshCw size={14}/> Restore into verification copy</Button></div>{result && <div className="alert success" role="status"><Check size={16}/><div><strong>{result.verified ? 'Backup and restore verified' : 'Verification incomplete'}</strong><p>{result.backup_id} · {result.evidence_objects} evidence object{result.evidence_objects === 1 ? '' : 's'}{result.generation ? ` · restored generation ${result.generation}` : ''}</p></div></div>}</Card></div>
}
