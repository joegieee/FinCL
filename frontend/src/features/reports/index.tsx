import { useCallback, useEffect, useState } from 'react'
import { ReportChart } from './ReportChart'
import { AlertTriangle, Download, FileBarChart, Pencil, Plus, Trash2, X } from 'lucide-react'
import { AsyncView } from '@/components/AsyncView'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { dateLabel } from '@/lib/format'
import { useLedgerCommand } from '@/lib/command'
import { reportedMoney, WorkspaceReportingDetails } from '@/lib/workspaceReporting'
import {
  COLUMN_LABEL, EXPORT_LABEL, GROUP_LABEL, exportReport, loadReports, loadResult, loadTripNames, previewResult,
  type Column, type ExportFormat, type GroupKey, type ReportDefinition, type ReportResult, type SavedReport, type Window,
} from './api'

type Saved = (status: string) => void | Promise<void>
const GROUP_KEYS = Object.keys(GROUP_LABEL) as GroupKey[]
const COLUMNS = Object.keys(COLUMN_LABEL) as Column[]

function windowLabel(window: Window): string {
  if (window.kind === 'all') return 'All time'
  if (window.kind === 'recent_months') return `Last ${window.months} month${window.months === 1 ? '' : 's'}`
  return `${dateLabel(window.start)} – ${dateLabel(window.end)}`
}

function describe(report: ReportDefinition): string {
  const by = report.group_by.length ? `by ${report.group_by.map(k => GROUP_LABEL[k].toLowerCase()).join(' and ')}` : 'in total'
  const filters = (['categories', 'accounts', 'trips', 'countries', 'currencies'] as const)
    .filter(name => report.scope[name].length).map(name => `${report.scope[name].length} ${name}`)
  return `${windowLabel(report.scope.window)} · ${by}${filters.length ? ` · only ${filters.join(', ')}` : ''}`
}

/**
 * Reports (v0.7). A saved report keeps its definition — window, filters,
 * grouping and columns — never its figures: opening it evaluates it against
 * the current data and says on which date. Filters keep identity, and one that
 * no longer matches anything is shown as a warning instead of being dropped.
 */
export function ReportsView({ generation, onSaved, categories, accounts, currencies }: {
  generation: string; onSaved: Saved; categories: string[]; accounts: string[]; currencies: string[]
}) {
  const [reports, setReports] = useState<SavedReport[] | null>(null)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const [editing, setEditing] = useState<SavedReport | 'new' | null>(null)

  const load = useCallback(() => {
    setError('')
    loadReports().then(r => setReports(r.reports)).catch(e => setError((e as Error).message))
  }, [])
  useEffect(() => { load() }, [load, generation])

  async function saved(status: string) { setEditing(null); load(); await onSaved(status) }
  const open = reports?.find(r => r.report_id === selected) ?? null

  return <div className="reports-view">
    <AsyncView loading={!reports && !error} error={error} data={reports} onRetry={load} errorTitle="We couldn’t load your reports.">
      {list => <>
        <div className="row-actions"><Button onClick={() => { setEditing('new'); setSelected(null) }}><Plus size={15}/> New report</Button></div>
        {editing && <ReportForm key={editing === 'new' ? 'new' : editing.report_id} initial={editing === 'new' ? null : editing}
          generation={generation} categories={categories} accounts={accounts} currencies={currencies}
          onSaved={saved} onCancel={() => setEditing(null)}/>}
        {list.length === 0 && !editing && <Card className="controls-card"><p className="muted-note report-empty">No saved reports yet. A report remembers a window, filters and grouping, and shows the current figures each time you open it.</p></Card>}
        <ul className="report-list">{list.map(report => <li key={report.report_id}>
          <button className={`report-card${selected === report.report_id ? ' is-active' : ''}`} aria-pressed={selected === report.report_id}
            onClick={() => { setSelected(report.report_id); setEditing(null) }}>
            <strong><FileBarChart size={15}/> {report.name}</strong>
            <small>{describe(report)}</small>
          </button>
        </li>)}</ul>
        {open && !editing && <ReportPanel key={open.report_id} report={open} generation={generation}
          onEdit={() => setEditing(open)} onClose={() => setSelected(null)}
          onRetired={async status => { setSelected(null); await saved(status) }}/>}
      </>}
    </AsyncView>
  </div>
}

function ReportPanel({ report, generation, onEdit, onClose, onRetired }: {
  report: SavedReport; generation: string; onEdit: () => void; onClose: () => void; onRetired: Saved
}) {
  const [result, setResult] = useState<ReportResult | null>(null)
  const [error, setError] = useState('')
  const [deleting, setDeleting] = useState(false)
  const [reason, setReason] = useState('')
  const cmd = useLedgerCommand(generation, onRetired)
  const load = useCallback(() => {
    setError('')
    loadResult(report.report_id).then(setResult).catch(e => setError((e as Error).message))
  }, [report.report_id])
  useEffect(() => { load() }, [load, generation])

  return <Card className="controls-card report-panel">
    <div className="panel-title">
      <div><p className="eyebrow">Saved report</p><h2>{report.name}</h2></div>
      <div className="row-actions">
        <Button variant="outline" size="sm" onClick={onEdit}><Pencil size={14}/> Edit</Button>
        <Button variant="outline" size="sm" onClick={() => setDeleting(true)}><Trash2 size={14}/> Delete</Button>
        <Button variant="ghost" size="sm" onClick={onClose} aria-label="Close report"><X size={15}/></Button>
      </div>
    </div>
    <div className="report-body">
      {deleting && <div className="report-delete">
        <label>Why delete it?<input value={reason} onChange={e => { setReason(e.target.value); cmd.reset() }}/></label>
        {cmd.previewed && <p role="status" className="muted-note">Deletes the saved definition “{report.name}”. Your transactions are not touched.</p>}
        {cmd.error && <p className="form-error" role="alert">{cmd.error}</p>}
        <div className="row-actions">
          <Button variant="ghost" size="sm" onClick={() => setDeleting(false)}>Cancel</Button>
          <Button size="sm" disabled={!reason.trim() || cmd.busy}
            onClick={() => void cmd.act({ kind: 'REPORT_DEFINITION_RETIRE', anchors: { report: report.report_id }, payload: { reason: reason.trim() } })}>
            {cmd.previewed ? 'Delete report' : 'Preview delete'}
          </Button>
        </div>
      </div>}
      <AsyncView loading={!result && !error} error={error} data={result} onRetry={load} errorTitle="We couldn’t run this report.">
        {data => <><ResultTable result={data} columns={report.columns}/><ExportControls source={{ report_id: report.report_id }} generation={data.generation}/></>}
      </AsyncView>
    </div>
  </Card>
}

/** Choose a format and download; the file is built from one snapshot with a manifest. */
function ExportControls({ source, generation }: { source: Parameters<typeof exportReport>[1]; generation?: string }) {
  const [format, setFormat] = useState<ExportFormat>('csv')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  async function run() {
    setBusy(true); setError(''); setMessage('')
    try { setMessage(`Downloaded ${await exportReport(format, source, generation)}.`) } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }
  return <div className="report-export">
    <label>Export as<select value={format} onChange={e => setFormat(e.target.value as ExportFormat)}>
      {(Object.keys(EXPORT_LABEL) as ExportFormat[]).map(f => <option key={f} value={f}>{EXPORT_LABEL[f]}</option>)}
    </select></label>
    <Button variant="outline" size="sm" disabled={busy} onClick={() => void run()}><Download size={14}/> {busy ? 'Preparing…' : 'Download'}</Button>
    <p className="muted-note">Contains only this report’s rows and a manifest of how they were worked out — no statements or decision history.</p>
    {message && <p className="muted-note" role="status">{message}</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
  </div>
}

export function ResultTable({ result, columns }: { result: ReportResult; columns: Column[] }) {
  const keys = (result.report.group_by ?? []) as GroupKey[]
  const window = result.window.start ? `${dateLabel(result.window.start)} – ${dateLabel(result.window.end!)}` : 'All time'
  return <div className="report-result">
    <WorkspaceReportingDetails data={result}/>
    <p className="muted-note">{window} · figures as of {dateLabel(result.evaluated_on)}{result.data_through && ` · your recorded transactions run through ${dateLabel(result.data_through)}`}. {result.lens_note}</p>
    {result.warnings.length > 0 && <ul className="issue-list report-warnings">
      {result.warnings.map((warning, i) => <li key={i}><p><AlertTriangle size={13}/> {warning}</p></li>)}
    </ul>}
    <ReportChart result={result} columns={columns} />
    {result.rows.length === 0
      ? <p className="muted-note">Nothing matches this report.</p>
      : <div className="table-scroll"><table className="report-table">
        <thead><tr>
          {keys.map(key => <th key={key} scope="col">{GROUP_LABEL[key]}</th>)}
          <th scope="col">Currency</th>
          {columns.map(column => <th key={column} scope="col" className="num">{COLUMN_LABEL[column]}</th>)}
        </tr></thead>
        <tbody>
          {result.rows.map((row, i) => <tr key={i}>
            {row.group.map(g => <td key={g.key}>{g.label}</td>)}
            <td>{row.currency}</td>
            {columns.map(column => <td key={column} className="num">
              {column === 'entry_count' ? row.entry_count : reportedMoney(row, column, row.currency)}
            </td>)}
          </tr>)}
        </tbody>
        <tfoot>
          {Object.entries(result.totals_by_currency).map(([currency, total]) => <tr key={currency}>
            {keys.length > 0 && <th scope="row" colSpan={keys.length}>Total</th>}
            <td>{keys.length ? currency : <strong>Total · {currency}</strong>}</td>
            {columns.map(column => <td key={column} className="num">
              <strong>{column === 'entry_count' ? total.entry_count : reportedMoney(total, column, currency)}</strong>
            </td>)}
          </tr>)}
        </tfoot>
      </table></div>}
  </div>
}

function toggle<T>(list: T[], value: T): T[] {
  return list.includes(value) ? list.filter(v => v !== value) : [...list, value]
}

function codes(text: string): string[] {
  return [...new Set(text.split(/[\s,]+/).map(c => c.trim().toUpperCase()).filter(Boolean))]
}

function ReportForm({ initial, generation, categories, accounts, currencies, onSaved, onCancel }: {
  initial: SavedReport | null; generation: string; categories: string[]; accounts: string[]; currencies: string[]
  onSaved: Saved; onCancel: () => void
}) {
  const [reportId] = useState(() => initial?.report_id ?? crypto.randomUUID())
  const [name, setName] = useState(initial?.name ?? '')
  const [windowKind, setWindowKind] = useState<Window['kind']>(initial?.scope.window.kind ?? 'recent_months')
  const [months, setMonths] = useState(initial?.scope.window.kind === 'recent_months' ? initial.scope.window.months : 3)
  const [start, setStart] = useState(initial?.scope.window.kind === 'fixed' ? initial.scope.window.start : '')
  const [end, setEnd] = useState(initial?.scope.window.kind === 'fixed' ? initial.scope.window.end : '')
  const [groupBy, setGroupBy] = useState<GroupKey[]>(initial?.group_by ?? ['category'])
  const [columns, setColumns] = useState<Column[]>(initial?.columns ?? ['gross_cost', 'refunds', 'net_cost'])
  const [chosenCategories, setChosenCategories] = useState<string[]>(initial?.scope.categories ?? [])
  const [chosenAccounts, setChosenAccounts] = useState<string[]>(initial?.scope.accounts ?? [])
  const [chosenCurrencies, setChosenCurrencies] = useState<string[]>(initial?.scope.currencies ?? [])
  const [chosenTrips, setChosenTrips] = useState<string[]>(initial?.scope.trips ?? [])
  const [countries, setCountries] = useState((initial?.scope.countries ?? []).join(', '))
  const [trips, setTrips] = useState<{ trip_id: string; name: string }[]>([])
  const [reason, setReason] = useState('')
  const [preview, setPreview] = useState<ReportResult | null>(null)
  const [previewError, setPreviewError] = useState('')
  const cmd = useLedgerCommand(generation, onSaved)
  useEffect(() => { loadTripNames().then(r => setTrips(r.trips)).catch(() => {}) }, [])

  const window: Window = windowKind === 'all' ? { kind: 'all' }
    : windowKind === 'fixed' ? { kind: 'fixed', start, end } : { kind: 'recent_months', months }
  const definition: ReportDefinition = {
    name: name.trim(), lens: 'recorded_flow', group_by: groupBy, columns,
    scope: { window, categories: chosenCategories, accounts: chosenAccounts, currencies: chosenCurrencies, trips: chosenTrips, countries: codes(countries) },
  }
  const windowOk = windowKind !== 'fixed' || Boolean(start && end && start <= end)
  const shapeOk = Boolean(definition.name && columns.length && windowOk && months >= 1 && months <= 36)
  const ready = shapeOk && Boolean(reason.trim())

  function change<T>(set: (value: T) => void) {
    return (value: T) => { set(value); cmd.reset(); setPreview(null) }
  }
  async function run() {
    setPreviewError('')
    try { setPreview(await previewResult(definition)) } catch (e) { setPreviewError((e as Error).message) }
  }

  return <Card className="controls-card report-form">
    <div className="panel-title"><div><p className="eyebrow">{initial ? 'Edit report' : 'New report'}</p><h2>{initial ? initial.name : 'What to show'}</h2></div>
      <Button variant="ghost" size="sm" onClick={onCancel} aria-label="Close report form"><X size={15}/></Button></div>
    <div className="report-body">
      <div className="form-grid">
        <label>Name<input value={name} maxLength={80} onChange={e => change(setName)(e.target.value)}/></label>
        <label>Period<select value={windowKind} onChange={e => change(setWindowKind)(e.target.value as Window['kind'])}>
          <option value="recent_months">Recent months</option><option value="fixed">Fixed dates</option><option value="all">All time</option>
        </select></label>
        {windowKind === 'recent_months' && <label>Months<input type="number" min={1} max={36} value={months} onChange={e => change(setMonths)(Number(e.target.value))}/></label>}
        {windowKind === 'fixed' && <>
          <label>From<input type="date" value={start} onChange={e => change(setStart)(e.target.value)}/></label>
          <label>To<input type="date" value={end} onChange={e => change(setEnd)(e.target.value)}/></label>
        </>}
      </div>
      {windowKind === 'fixed' && start && end && start > end && <p className="form-error" role="alert">The end is before the start.</p>}

      <fieldset className="report-choices"><legend>Group by (up to 2, in order)</legend>
        {GROUP_KEYS.map(key => <label key={key}><input type="checkbox" checked={groupBy.includes(key)}
          disabled={!groupBy.includes(key) && groupBy.length >= 2} onChange={() => change(setGroupBy)(toggle(groupBy, key))}/> {GROUP_LABEL[key]}</label>)}
      </fieldset>
      <fieldset className="report-choices"><legend>Columns</legend>
        {COLUMNS.map(column => <label key={column}><input type="checkbox" checked={columns.includes(column)}
          onChange={() => change(setColumns)(COLUMNS.filter(c => c === column ? !columns.includes(c) : columns.includes(c)))}/> {COLUMN_LABEL[column]}</label>)}
      </fieldset>

      <details className="report-filters" open={Boolean(initial && Object.values(initial.scope).some(v => Array.isArray(v) && v.length))}>
        <summary>Only include… (optional)</summary>
        <div className="form-grid">
          <label>Categories<select multiple value={chosenCategories} onChange={e => change(setChosenCategories)([...e.target.selectedOptions].map(o => o.value))}>
            {[...new Set([...categories, ...chosenCategories])].map(c => <option key={c} value={c}>{c}</option>)}
          </select></label>
          <label>Accounts<select multiple value={chosenAccounts} onChange={e => change(setChosenAccounts)([...e.target.selectedOptions].map(o => o.value))}>
            {[...new Set([...accounts, ...chosenAccounts])].map(a => <option key={a} value={a}>{a}</option>)}
          </select></label>
          <label>Currencies<select multiple value={chosenCurrencies} onChange={e => change(setChosenCurrencies)([...e.target.selectedOptions].map(o => o.value))}>
            {[...new Set([...currencies, ...chosenCurrencies])].map(c => <option key={c} value={c}>{c}</option>)}
          </select></label>
          <label>Trips<select multiple value={chosenTrips} onChange={e => change(setChosenTrips)([...e.target.selectedOptions].map(o => o.value))}>
            {trips.map(t => <option key={t.trip_id} value={t.trip_id}>{t.name}</option>)}
            {chosenTrips.filter(id => !trips.some(t => t.trip_id === id)).map(id => <option key={id} value={id}>Removed trip</option>)}
          </select></label>
          <label>Countries<input value={countries} placeholder="JP, KR" onChange={e => change(setCountries)(e.target.value)}/></label>
        </div>
        <p className="muted-note">Leave a list empty to include everything. Hold Ctrl (or ⌘) to pick several.</p>
      </details>

      <div className="row-actions"><Button variant="outline" disabled={!shapeOk} onClick={() => void run()}>Show result</Button></div>
      {previewError && <p className="form-error" role="alert">{previewError}</p>}
      {preview && <><ResultTable result={preview} columns={columns}/><ExportControls source={{ definition }} generation={preview.generation}/></>}

      <div className="form-grid">
        <label>Reason<input value={reason} placeholder="Why keep this report" onChange={e => change(setReason)(e.target.value)}/></label>
      </div>
      {cmd.previewed && <p className="muted-note" role="status">{initial ? 'Updates' : 'Saves'} “{definition.name}”: {describe(definition)}. Only the definition is kept; figures are worked out each time it opens.</p>}
      {cmd.error && <p className="form-error" role="alert">{cmd.error}</p>}
      <div className="row-actions"><Button disabled={!ready || cmd.busy}
        onClick={() => void cmd.act({ kind: 'REPORT_DEFINITION_SET', anchors: { report: reportId }, payload: { ...definition, reason: reason.trim() } })}>
        {cmd.busy ? 'Working…' : cmd.previewed ? 'Save report' : 'Preview save'}
      </Button></div>
    </div>
  </Card>
}
