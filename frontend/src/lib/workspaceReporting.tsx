import { api, money as nativeMoney, type components } from './api'
import { Money } from '@/components/Money'

type Envelope = components['schemas']['ReportedWorkspace']
const fields = new WeakMap<object, Map<string, components['schemas']['ValuedMoneyDTO']>>()
const envelopes = new WeakMap<object, Envelope>()

/** Keep reporting metadata outside native objects so spreads/JSON commands
 * contain exactly the original native amount, currency and predicate. */
export async function reportedApi<T>(path: string, body?: unknown): Promise<T> {
  const response = body === undefined ? await api<Envelope>(`reporting/workspaces/${path}`) : await api<Envelope>(`reporting/workspaces/${path}`, body)
  // Native-shaped fixtures remain useful in component tests.
  if (!response || !('native' in response)) return response as T
  const register = (value: unknown) => {
    if (!value || typeof value !== 'object') return
    fields.set(value, new Map())
    for (const child of Object.values(value)) register(child)
  }
  register(response.native)
  for (const annotation of response.amounts) {
    let owner: any = response.native
    for (const segment of annotation.path.slice(0, -1)) owner = owner?.[segment]
    if (!owner || typeof owner !== 'object') continue
    let values = fields.get(owner)
    if (!values) { values = new Map(); fields.set(owner, values) }
    values.set(String(annotation.path.at(-1)), annotation.value)
  }
  envelopes.set(response.native, response)
  return response.native as T
}

function grouped(value: string): string {
  const [whole, fraction] = value.split('.')
  return `${whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',')}${fraction === undefined ? '' : `.${fraction}`}`
}

/** No client-side conversions, sums or rounding. Lookup uses the owning
 * object and field, never amount equality (which loses date and provenance). */
export function reportedMoney(owner: any, field: string, currency: string): string {
  const value = owner && fields.get(owner)?.get(field)
  if (!value) {
    if (owner && fields.has(owner) && owner[field] != null) return 'Unavailable · This amount needs a dated reporting basis.'
    return nativeMoney(owner?.[field] ?? null, currency)
  }
  // The original amount stays visible: unknown in the reporting currency is not unknown.
  if (value.unavailable) return `Unavailable · ${grouped(value.native.value)} ${value.native.currency} not converted. ${value.unavailable.message} ${value.unavailable.remedy}`
  const amount = value.reported ?? value.native
  return `${grouped(amount.value)} ${amount.currency}`
}

// Long reads (e.g. every commitment event) would otherwise render thousands of rows.
const DETAIL_LIMIT = 60

export function WorkspaceReportingDetails({ data }: { data: object }) {
  const report = envelopes.get(data)
  if (!report) return null
  const hidden = Math.max(report.amounts.length - DETAIL_LIMIT, 0)
  return <details className="workspace-reporting-details">
    <summary>Reporting in {report.reporting_currency} · valuation and native evidence</summary>
    <p>Each amount keeps its own date. Converted targets and balances are reference values; future amounts are estimates. Original amounts remain unchanged for edits and statement checks.</p>
    {hidden > 0 && <p className="muted-note">Showing the first {DETAIL_LIMIT} of {report.amounts.length} amounts; each one is also shown where it appears on this page.</p>}
    <ul className="issue-list">{report.amounts.slice(0, DETAIL_LIMIT).map((item, index) => <li key={index}>
      <span>{item.label} · {({ flow: 'Recorded amount', position: 'Balance or target reference', forecast: 'Forecast estimate' })[item.value.purpose]} · {item.value.valuation_as_of}</span>{' '}
      <Money value={item.value} />
    </li>)}</ul>
  </details>
}
