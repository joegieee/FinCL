import type { components } from './schema'
export type { components }
export type Dashboard = components['schemas']['Dashboard']
export type Entry = components['schemas']['Entry']
export type Plan = components['schemas']['Plan']
export type Proposal = components['schemas']['Proposal']
export type TransactionsPage = components['schemas']['TransactionsPage']
export type Command = components['schemas']['Command']
export type Analysis = components['schemas']['Analysis']
export type SummaryCell = components['schemas']['SummaryCell']
export type CategoryChange = components['schemas']['CategoryChange']
export type MerchantRank = components['schemas']['MerchantRank']
export type TrendPoint = components['schemas']['TrendPoint']
export type ContributorsPage = components['schemas']['ContributorsPage']
export type Contributor = components['schemas']['Contributor']
let csrf = ''
export class ApiError extends Error { constructor(message:string,public status:number){super(message)} }
// FinCLError payload-validation messages are internal shape descriptions
// (`invalid categorization payload: category must be a non-empty string`),
// not user-facing copy. Strip the technical prefix so the UI shows the
// actual problem in plain language.
const ERROR_PREFIXES: [RegExp, string][] = [
  [/^invalid categorization payload: /, ''],
  [/^invalid regex alias pattern: /, 'That pattern is not valid: '],
]
function humanizeError(message:string):string {
  for (const [pattern, replacement] of ERROR_PREFIXES) {
    if (pattern.test(message)) { const rest = message.replace(pattern, replacement); return rest.charAt(0).toUpperCase() + rest.slice(1) }
  }
  return message
}
// In-flight GET requests are shared by identical method+path+body key, so
// switching tabs or re-rendering while a request is outstanding issues one
// fetch instead of a duplicate. This is dedup
// only, not a time-based cache: a completed GET is not replayed for a later
// call, since the right invalidation trigger (generation vs. coverage-only
// changes from Setup imports and refresh) is still an open question there.
const inFlightGets = new Map<string, Promise<unknown>>()
export async function api<T>(path:string, body?:unknown, options?:{signal?:AbortSignal}):Promise<T> {
  const method = body===undefined?'GET':'POST'
  const key = method==='GET' ? path : ''
  if (key && inFlightGets.has(key)) return inFlightGets.get(key) as Promise<T>
  const promise = (async () => {
    const response = await fetch(`/api/v1/${path}`,{method,headers:body===undefined?{}:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:body===undefined?undefined:JSON.stringify(body),signal:options?.signal})
    const value = await response.json()
    if (!response.ok) throw new ApiError(typeof value.detail==='string'?humanizeError(value.detail):'Please check the entered values.',response.status)
    return value as T
  })()
  if (key) { inFlightGets.set(key, promise); const cleanup = () => inFlightGets.delete(key); promise.then(cleanup, cleanup) }
  return promise
}
// The one multipart write. Same Origin/session/CSRF boundary as JSON writes.
export async function uploadFiles<T>(path:string, adapter:string, files:File[]):Promise<T> {
  const form = new FormData()
  form.append('adapter', adapter)
  for (const file of files) form.append('files', file, file.name)
  const response = await fetch(`/api/v1/${path}`,{method:'POST',headers:{'X-CSRF-Token':csrf},body:form})
  const value = await response.json()
  if (!response.ok) throw new ApiError(typeof value.detail==='string'?value.detail:'That upload could not be accepted.',response.status)
  return value as T
}
// The other multipart write: a single reference-data file plus scalar form
// fields, distinct from the many-files statement upload above.
export async function uploadFxRates<T>(file:File, fields:Record<string,string>):Promise<T> {
  const form = new FormData()
  form.append('file', file, file.name)
  for (const [key, value] of Object.entries(fields)) form.append(key, value)
  const response = await fetch('/api/v1/fx-rates',{method:'POST',headers:{'X-CSRF-Token':csrf},body:form})
  const value = await response.json()
  if (!response.ok) throw new ApiError(typeof value.detail==='string'?value.detail:'That upload could not be accepted.',response.status)
  return value as T
}
// A file the server builds and the browser saves. Same Origin/session/CSRF
// boundary as JSON writes; the server never writes to a path of its own.
export async function downloadFile(path:string, body:unknown):Promise<string> {
  const response = await fetch(`/api/v1/${path}`,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body)})
  if (!response.ok) {
    const value = await response.json().catch(() => ({}))
    throw new ApiError(typeof value.detail==='string'?humanizeError(value.detail):'That file could not be made.',response.status)
  }
  const name = /filename="([^"]+)"/.exec(response.headers.get('Content-Disposition') ?? '')?.[1] ?? 'download'
  const url = URL.createObjectURL(await response.blob())
  const link = document.createElement('a')
  link.href = url; link.download = name; document.body.appendChild(link); link.click(); link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 0)
  return name
}
let boot: Promise<{csrf:string;demo:boolean;label:string}> | undefined
export function bootstrap() { return boot ??= doBootstrap() }
async function doBootstrap() {
  const secret = new URLSearchParams(location.hash.slice(1)).get('bootstrap')
  if (secret) {
    history.replaceState(null,'',location.pathname+location.search)
    const response = await fetch('/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({secret})})
    if(!response.ok) throw new Error('This launch link has expired. Reopen the link from fincl serve.')
  }
  const health = await api<{csrf:string;demo:boolean;label:string}>('health')
  csrf=health.csrf
  return health
}
// Never pass authoritative decimal strings through Number, including large balances.
export function money(value:string|null|undefined,currency='SGD'):string {
  if(value==null) return 'Unavailable'
  const negative=value.startsWith('-'); const [whole,fraction='']=value.replace(/^[+-]/,'').split('.')
  return `${negative?'−':''}${currency} ${whole.replace(/\B(?=(\d{3})+(?!\d))/g,',')}.${fraction.padEnd(2,'0')}`
}
export function shortCategory(value:string) { return value.split(' / ').at(-1) || value }

export function compareMoney(a:string,b:string):number {
 const scale=Math.max(a.split('.')[1]?.length||0,b.split('.')[1]?.length||0)
 const integer=(v:string)=>{const [whole,dec='']=v.split('.');return BigInt(whole+dec.padEnd(scale,'0'))}
 const left=integer(a),right=integer(b);return left<right?-1:left>right?1:0
}
