import type { components } from './schema'
export type Dashboard = components['schemas']['Dashboard']
export type Entry = components['schemas']['Entry']
export type Plan = components['schemas']['Plan']
export type Proposal = components['schemas']['Proposal']
export type TransactionsPage = components['schemas']['TransactionsPage']
export type Command = components['schemas']['Command']
let csrf = ''
export class ApiError extends Error { constructor(message:string,public status:number){super(message)} }
export async function api<T>(path:string, body?:unknown):Promise<T> {
  const response = await fetch(`/api/v1/${path}`,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:body===undefined?undefined:JSON.stringify(body)})
  const value = await response.json()
  if (!response.ok) throw new ApiError(typeof value.detail==='string'?value.detail:'Please check the entered values.',response.status)
  return value as T
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
