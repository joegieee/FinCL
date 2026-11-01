import { useCallback, useEffect, useRef, useState } from 'react'
import { Check, Clock, Undo2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { AsyncView } from '@/components/AsyncView'
import { api } from '@/lib/api'
import { useLedgerCommand } from '@/lib/command'
import { humanize } from '@/lib/limitations'

type Issue = {
  queue: string
  issue_key: string
  status: string
  title: string
  detail: string
  owner: string
  fingerprint: string
  deferred: boolean
}
type ReviewPage = { generation: string; issues: Issue[] }
type HistoryRow = { sequence: number; date: string; kind: string; reason: string; category: string }
type InversePreview =
  | { supported: false; reason: string }
  | { supported: true; generation: string; command: { kind: string; anchors: Record<string, string>; payload: Record<string, unknown> } }

const QUEUE_LABEL: Record<string, string> = {
  source: 'Source & coverage',
  categories: 'Categories & inflows',
  relationships: 'Ambiguous relationships',
  debts: 'Debts & cards',
}

/**
 * v0.5 slice 4 — the Review hub. Independent queues; each count is a number of
 * issues, never a money total, and queues are never summed. "Later" is a
 * journal-backed deferral for the issue's current fingerprint, not a deletion:
 * a changed issue resurfaces. Undo selects a past decision and previews its
 * supported inverse against the current generation.
 */
export function ReviewHub({ freshness, onSaved, onOpenCategorization }: { freshness: string; onSaved: (status: string) => void | Promise<void>; onOpenCategorization?: () => void }) {
  const [page, setPage] = useState<ReviewPage | null>(null)
  const [error, setError] = useState('')
  const [showDeferred, setShowDeferred] = useState(false)
  const [unclassified, setUnclassified] = useState<number | null>(null)
  const seq = useRef(0)

  const load = useCallback(() => {
    const id = ++seq.current
    setError('')
    api<ReviewPage>('review')
      .then(p => { if (id === seq.current) setPage(p) })
      .catch(e => { if (id === seq.current) setError((e as Error).message) })
  }, [])
  useEffect(() => { load() }, [load])
  useEffect(() => {
    if (!page) return
    api<{ counts: Record<string, number> }>(`categorization/summary?generation=${encodeURIComponent(page.generation)}`)
      .then(summary => setUnclassified(summary.counts.unclassified_active || 0))
      .catch(() => setUnclassified(null))
  }, [page])

  const afterSave = async (status: string) => { await onSaved(status); load() }
  const disabled = freshness === 'stale'

  return (
    <AsyncView loading={!page && !error} error={error} data={page} onRetry={load} loadingLabel="Loading review" skeletonRows={2} errorTitle="We couldn’t load the review hub.">
      {page => <ReviewHubContent page={page} disabled={disabled} afterSave={afterSave} unclassified={unclassified} onOpenCategorization={onOpenCategorization} showDeferred={showDeferred} setShowDeferred={setShowDeferred} />}
    </AsyncView>
  )
}

function ReviewHubContent({ page, disabled, afterSave, unclassified, onOpenCategorization, showDeferred, setShowDeferred }: {
  page: ReviewPage; disabled: boolean; afterSave: (status: string) => Promise<void>; unclassified: number | null
  onOpenCategorization?: () => void; showDeferred: boolean; setShowDeferred: (fn: (v: boolean) => boolean) => void
}) {
  const active = page.issues.filter(i => !i.deferred)
  const deferred = page.issues.filter(i => i.deferred)
  const queues = [...new Set(active.map(i => i.queue))]

  return (
    <div className="review-hub">
      <div className="queue-summary">
        {queues.length === 0 && <span className="covered"><span className="status-dot" /> No open review issues</span>}
        {queues.map(q => (
          <Badge key={q} variant="outline">{QUEUE_LABEL[q] ?? q}: {active.filter(i => i.queue === q).length}</Badge>
        ))}
        {deferred.length > 0 && (
          <Button variant="ghost" size="sm" onClick={() => setShowDeferred(v => !v)}>
            <Clock size={13} /> {showDeferred ? 'Hide' : 'Show'} {deferred.length} deferred
          </Button>
        )}
      </div>
      <p className="muted-note">Each number counts issues in one queue. Queues are independent and never added together.</p>
      {unclassified !== null && <Card className="review-categorization-summary"><div><p className="eyebrow">Categorization backlog</p><h2>{unclassified} active transaction{unclassified === 1 ? ' needs' : 's need'} a category, across all dates</h2><p>Ordinary unclassified transactions are grouped in Categorization, not added one by one to Review.</p></div>{onOpenCategorization && <Button variant="outline" onClick={onOpenCategorization}>Open Inbox</Button>}</Card>}

      {queues.map(queue => (
        <Card key={queue} className="review-queue">
          <div className="panel-title"><div><p className="eyebrow">{(QUEUE_LABEL[queue] ?? queue)}</p><h2>{active.filter(i => i.queue === queue).length} to look at</h2></div></div>
          {active.filter(i => i.queue === queue).map(issue => (
            <IssueRow key={issue.issue_key} issue={issue} generation={page.generation} disabled={disabled} onSaved={afterSave} />
          ))}
        </Card>
      ))}

      {showDeferred && deferred.length > 0 && (
        <Card className="review-queue">
          <div className="panel-title"><div><p className="eyebrow">Deferred</p><h2>{deferred.length} set aside</h2></div></div>
          {deferred.map(issue => (
            <IssueRow key={issue.issue_key} issue={issue} generation={page.generation} disabled={disabled} onSaved={afterSave} />
          ))}
        </Card>
      )}

      <UndoPanel generation={page.generation} disabled={disabled} onSaved={afterSave} />
    </div>
  )
}

function IssueRow({ issue, generation, disabled, onSaved }: {
  issue: Issue; generation: string; disabled: boolean; onSaved: (status: string) => void | Promise<void>
}) {
  const cmd = useLedgerCommand(generation, onSaved)
  const action = issue.deferred
    ? { kind: 'REVIEW_DEFER_RETIRE', payload: { reason: `Resurface ${issue.issue_key}` } }
    : { kind: 'REVIEW_DEFER_SET', payload: { fingerprint: issue.fingerprint, reason: `Defer ${issue.issue_key}` } }
  return (
    <div className="issue-row">
      <div>
        <Badge variant="outline" className={issue.status === 'BLOCKING' ? 'review-badge' : ''}>{humanize(issue.status)}</Badge>
        <strong>{issue.title}</strong>
        <p>{issue.detail}</p>
        <small className="mono">{issue.issue_key}</small>
      </div>
      <div className="row-actions">
        <Button variant="ghost" size="sm" disabled={disabled || cmd.busy}
          onClick={() => void cmd.act({ kind: action.kind, anchors: { issue: issue.issue_key }, payload: action.payload })}>
          {cmd.previewed && !cmd.attempted ? 'Confirm' : cmd.busy ? 'Working…' : issue.deferred ? 'Undefer' : 'Later'}
        </Button>
      </div>
      {cmd.error && <p className="form-error" role="alert">{cmd.error}</p>}
    </div>
  )
}

function UndoPanel({ generation, disabled, onSaved }: {
  generation: string; disabled: boolean; onSaved: (status: string) => void | Promise<void>
}) {
  const [history, setHistory] = useState<HistoryRow[]>([])
  const [sequence, setSequence] = useState('')
  const [preview, setPreview] = useState<InversePreview | null>(null)
  const [error, setError] = useState('')
  const cmd = useLedgerCommand(generation, async s => { setPreview(null); setSequence(''); await onSaved(s) })

  useEffect(() => { api<HistoryRow[]>('history').then(setHistory).catch(() => setHistory([])) }, [])

  async function lookup() {
    setError(''); setPreview(null); cmd.reset()
    try {
      setPreview(await api<InversePreview>(`history/${encodeURIComponent(sequence)}/inverse-preview`))
    } catch (e) {
      setError((e as Error).message)
    }
  }

  return (
    <Card className="undo-panel">
      <div className="panel-title"><div><p className="eyebrow">Undo a decision</p><h2>Preview a supported inverse against the current ledger</h2></div></div>
      <form onSubmit={e => { e.preventDefault(); void lookup() }}>
        <label>Decision
          <select value={sequence} onChange={e => { setSequence(e.target.value); setPreview(null); cmd.reset() }}>
            <option value="">Choose a decision…</option>
            {history.map(row => <option key={row.sequence} value={row.sequence}>#{row.sequence} · {row.kind.toLowerCase().replaceAll('_', ' ')}{row.reason ? ` — ${row.reason}` : ''}</option>)}
          </select>
        </label>
        <div className="dialog-actions"><Button type="submit" disabled={!sequence}>Check inverse</Button></div>
      </form>
      {error && <p className="form-error" role="alert">{error}</p>}
      {preview && !preview.supported && <p className="muted-note"><Undo2 size={13} /> {preview.reason}</p>}
      {preview && preview.supported && (
        <div className="inverse-preview">
          <p><Undo2 size={13} /> Inverse: <code>{preview.command.kind}</code></p>
          {cmd.error && <p className="form-error" role="alert">{cmd.error}</p>}
          {cmd.previewed && <div className="preview" role="status"><Check size={15} /><p>Ready to append the inverse decision.</p></div>}
          {disabled && <p className="form-error">Refresh the ledger before undoing.</p>}
          <div className="dialog-actions">
            <Button type="button" disabled={disabled || cmd.busy}
              onClick={() => void cmd.act({ kind: preview.command.kind, anchors: preview.command.anchors, payload: preview.command.payload })}>
              {cmd.busy ? 'Working…' : cmd.previewed ? (cmd.attempted ? 'Retry same save' : 'Apply inverse') : 'Preview inverse'}
            </Button>
          </div>
        </div>
      )}
    </Card>
  )
}
