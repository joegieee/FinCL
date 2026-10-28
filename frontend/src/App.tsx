import { reportedApi, reportedMoney, WorkspaceReportingDetails } from '@/lib/workspaceReporting'
import { useEffect, useRef, useState } from 'react'
import { ArrowRight, BarChart3, Check, ChevronRight, CircleHelp, CreditCard, Layers3, Menu, Plus, RefreshCw, ShieldCheck, SlidersHorizontal, Target, Wallet, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { api, ApiError, bootstrap, money, shortCategory, type Command, type Dashboard, type Entry, type Plan, type Proposal } from '@/lib/api'
import { dateLabel } from '@/lib/format'
import { tabAliases, tabMeta } from '@/lib/tabs'
import { PeriodPicker } from '@/components/PeriodPicker'
import { NavigationRail } from '@/components/NavigationRail'
import { CommandPalette } from '@/components/CommandPalette'
import { PageHeader } from '@/components/PageHeader'
import { AnalysisView } from '@/features/analysis'
import { RelationshipEditor } from '@/features/review/RelationshipEditor'
import { DebtsView } from '@/features/debts'
import { TripsView } from '@/features/trips'
import { ReportsView } from '@/features/reports'
import { CardsView } from '@/features/cards'
import { InstallmentsView } from '@/features/installments'
import { ObligationsView } from '@/features/obligations'
import { NetWorthView } from '@/features/networth'
import { ReviewHub } from '@/features/review/ReviewHub'
import { SetupView } from '@/features/setup'
import { AccountsView } from '@/features/accounts'
import { RulesView } from '@/features/rules'
import { CustomCategoryManager } from '@/features/categories/CustomCategoryManager'
import { TransactionsView } from '@/features/transactions'
import { loadReportedTransactionDetail, type TransactionRowOut } from '@/features/transactions/reportedApi'
import { InflowsView } from '@/features/inflows'
import { EntryDialog } from '@/features/classification/SingleTransactionEditor'
import { PlanningView } from '@/features/planning'
import { BaselineView } from '@/features/baseline'
import { CashView } from '@/features/cash'
import { CategorizationView } from '@/features/categorization/CategorizationView'
import { ForecastEstimateSummary, ForecastView } from '@/features/forecast'
import type { ForecastPage } from '@/features/forecast/api'
import { AllControlsView } from '@/features/controls'
import { SufficiencyView } from '@/features/sufficiency'
import { OverviewView } from '@/features/overview'
import { categoryIconColor } from '@/features/overview/SpendingDonut'
import { FundsView } from '@/features/funds'
import { VarianceView } from '@/features/variance'
import { MaintenanceView } from '@/features/maintenance'

const tabs=['Overview','Baseline','Cash','Forecast','Will I have enough','Annual funds','Variance','Controls','Cash Flow','Analysis','Trips','Reports','Transactions','Income','Categorization','Debts','Cards','Installments','Money Owed','Obligations','Net worth','Review','Planning','Plans','Recurring','Categories','Rules','Accounts','Setup','Maintenance'] as const
type Tab=typeof tabs[number]
function initialTab():Tab { const v=new URLSearchParams(location.search).get('view');return tabs.find(t=>t===v)||'Overview' }
function pct(actual:string|null,target:string) { return actual===null?0:Math.max(0,Math.min(100,Number(actual)/Number(target)*100)) }

export default function App(){
 const [data,setData]=useState<Dashboard|null>(null), [tab,setTab]=useState<Tab>(initialTab), [error,setError]=useState(''), [notice,setNotice]=useState(''), [loading,setLoading]=useState(true)
 const [health,setHealth]=useState<{demo:boolean;label:string}|null>(null)
 const [start,setStart]=useState(new URLSearchParams(location.search).get('start')||''),[end,setEnd]=useState(new URLSearchParams(location.search).get('end')||''),[currency,setCurrency]=useState(new URLSearchParams(location.search).get('currency')||'SGD')
 // §6.3: an *explicit* `?currency=` URL value seeds the Analysis/Transactions
 // Account-currency filter once; `currency`'s own 'SGD' fallback (used by
 // every native single-pocket tab) is not itself such a value, so a fresh
 // visit with no URL parameter still shows every pocket by default there.
 const [initialUrlPocketCurrency]=useState(()=>new URLSearchParams(location.search).get('currency')||'')
 const [search,setSearch]=useState(''),[category,setCategory]=useState(''),[kind,setKind]=useState('')
 const [entry,setEntry]=useState<Entry|null>(null),[reportedRow,setReportedRow]=useState<TransactionRowOut|null>(null),[dialog,setDialog]=useState<'category'|'budget'|'goal'|null>(null),[editingPlan,setEditingPlan]=useState<Plan|null>(null)
 async function openReportedAnchor(encoded:string){try{setReportedRow(null);const row=await loadReportedTransactionDetail(encoded);setEntry(row.entry);setReportedRow(row)}catch(e){setError((e as Error).message)}}
 const [legacyData,setLegacyData]=useState<Dashboard|null>(null)
 useEffect(()=>{if(!data||!['Plans','Recurring','Review'].includes(tab))return;let live=true;setLegacyData(null);reportedApi<Dashboard>(`overview?start=${data.start}&end=${data.end}`).then(value=>{if(live)setLegacyData(value)}).catch(e=>{if(live)setError((e as Error).message)});return()=>{live=false}},[tab,data?.generation,data?.start,data?.end])
 const [historyRows,setHistoryRows]=useState<Array<{sequence:number;date:string;kind:string;reason:string;category:string}>|null>(null)
 const [forecastPage,setForecastPage]=useState<ForecastPage|undefined>(undefined)
 async function showHistory(){try{setHistoryRows(await api('history'))}catch(e){setError((e as Error).message)}}
 const [railCollapsed,setRailCollapsed]=useState(()=>{try{return localStorage.getItem('fincl.rail.collapsed')==='1'}catch{return false}})
 useEffect(()=>{try{localStorage.setItem('fincl.rail.collapsed',railCollapsed?'1':'0')}catch{/* private browsing or storage disabled */}},[railCollapsed])
 const [drawerOpen,setDrawerOpen]=useState(false)
 const [paletteOpen,setPaletteOpen]=useState(false)
 useEffect(()=>{function onKey(e:KeyboardEvent){if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){e.preventDefault();setPaletteOpen(o=>!o)}}document.addEventListener('keydown',onKey);return()=>document.removeEventListener('keydown',onKey)},[])
 const drawerRef=useRef<HTMLDivElement>(null)
 useEffect(()=>{
  if(!drawerOpen)return
  const focusable=()=>Array.from(drawerRef.current?.querySelectorAll<HTMLElement>('button,[href],input,select,textarea,[tabindex]:not([tabindex="-1"])')||[]).filter(el=>!el.hasAttribute('disabled'))
  focusable()[0]?.focus()
  function onKey(e:KeyboardEvent){
   if(e.key==='Escape'){setDrawerOpen(false);return}
   if(e.key!=='Tab')return
   const items=focusable();if(!items.length)return
   const first=items[0],last=items[items.length-1]
   if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus()}
   else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus()}
  }
  document.addEventListener('keydown',onKey)
  return()=>document.removeEventListener('keydown',onKey)
 },[drawerOpen])
 function goTo(t:string){setTab(t as Tab);setDrawerOpen(false)}
 const seq=useRef(0)
 async function load(s=start,e=end,c=currency){const id=++seq.current;setLoading(true);setError('');try{const q=new URLSearchParams({currency:c});if(s&&e){q.set('start',s);q.set('end',e)}const d=await api<Dashboard>('overview?'+q);if(id===seq.current){setData(d);setStart(d.start);setEnd(d.end);setCurrency(d.currency)}}catch(e){if(id===seq.current)setError((e as Error).message)}finally{if(id===seq.current)setLoading(false)}}
 useEffect(()=>{let mounted=true;bootstrap().then(h=>{if(mounted){setHealth(h);void load()}}).catch(e=>{if(mounted){setError(e.message);setLoading(false)}});return()=>{mounted=false}},[])
 useEffect(()=>{const params=new URLSearchParams({view:tab,currency});if(data){params.set('start',data.start);params.set('end',data.end)}history.replaceState(null,'','?'+params)},[tab,data,currency])
 function drill(cat='',merchant='',bucket=''){setCategory(cat);setSearch(merchant);setKind(bucket);setTab('Transactions')}
 async function saved(status:string){setNotice(status==='recorded_refresh_pending'?'Saved; totals awaiting refresh. Use Refresh to retry.':'Saved to your ledger.');setEntry(null);setReportedRow(null);setDialog(null);setEditingPlan(null);await load()}
 async function refresh(){setLoading(true);try{await api('refresh',{});await load();setNotice('Ledger refreshed.')}catch(e){setError((e as Error).message);setLoading(false)}}
 const reviewCount=data?.review_count||0
 const proposalCount=data?.transfer_proposals.length||0
 const meta=tabMeta[tab]
 const railActiveTab=tabAliases[tab]??tab
 return <div className="app-shell">
  <a className="skip-link" href="#main-content">Skip to content</a>
  <NavigationRail tab={railActiveTab} onSelect={goTo} reviewCount={reviewCount} proposalCount={proposalCount} collapsed={railCollapsed} onToggleCollapse={()=>setRailCollapsed(c=>!c)} ledgerLabel={health?.label||'Your money, understood'} onHistory={showHistory} historyDisabled={!data} onRefresh={refresh} refreshDisabled={loading||!data} refreshing={loading} demo={!!health?.demo}/>
  {drawerOpen&&<div className="rail-backdrop" onClick={()=>setDrawerOpen(false)}/>}
  {drawerOpen&&<div className="rail-drawer" ref={drawerRef} role="dialog" aria-modal="true" aria-label="Main navigation">
   <NavigationRail drawer tab={railActiveTab} onSelect={goTo} reviewCount={reviewCount} proposalCount={proposalCount} collapsed={false} onToggleCollapse={()=>{}} ledgerLabel={health?.label||'Your money, understood'} onHistory={showHistory} historyDisabled={!data} onRefresh={refresh} refreshDisabled={loading||!data} refreshing={loading} demo={!!health?.demo}/>
  </div>}
  <Button variant="ghost" size="sm" className="rail-drawer-toggle" aria-label="Open navigation" aria-expanded={drawerOpen} onClick={()=>setDrawerOpen(o=>!o)}><Menu size={18}/></Button>
  <div className="app-main-col">
  <main id="main-content" tabIndex={-1} className={tab==='Transactions'?'wide':''}>
   {error&&<div className="alert error" role="alert"><div><strong>We couldn’t complete that.</strong><p>{error}</p></div>{health&&<Button variant="outline" onClick={()=>load()}>Try again</Button>}</div>}
   {notice&&<div className="alert success" role="status"><span><Check size={15}/>{notice}</span><button aria-label="Dismiss message" onClick={()=>setNotice('')}><X size={16}/></button></div>}
   {!data&&loading?<div className="skeleton-page" aria-label="Loading dashboard"><div/><div/><div/></div>:!data?<Card className="empty"><Wallet/><h1>Open your personal ledger</h1><p>Launch FinCL locally, then open the website address. Your financial data stays on this computer.</p><code>fincl serve --database var/fincl.sqlite3</code></Card>:<>
   <PageHeader meta={meta} action={tab==='Categories'?<Button onClick={()=>setDialog('category')}><Plus size={16}/> New category</Button>:tab==='Plans'?<Button onClick={()=>{setEditingPlan(null);setDialog('budget')}}><Plus size={16}/> New budget</Button>:undefined}/>
   {meta.scope==='range'&&<><div className="scope-bar"><PeriodPicker start={data.start} end={data.end} applying={loading} onApply={(s,e)=>{setStart(s);setEnd(e);void load(s,e)}}/></div>
   {tab!=='Analysis'&&tab!=='Cash Flow'&&tab!=='Transactions'&&tab!=='Overview'&&tab!=='Categorization'&&<div className="scope-caption"><span>{dateLabel(data.start)} – {dateLabel(data.end)} · {data.currency} only · transfers excluded from spending</span><span className={data.covered?'covered':'coverage-missing'}><span className="status-dot"/>{data.covered?'Covered period':'Incomplete coverage'}</span></div>}</>}

   {data.freshness==='stale'&&<div className="alert" role="status"><span>New ledger inputs are available. Refresh before editing or interpreting these totals as current.</span><Button size="sm" variant="outline" disabled={loading} onClick={refresh}><RefreshCw size={14} className={loading?'spin':''}/> Refresh now</Button></div>}
   {meta.scope==='range'&&!data.covered&&<div className="alert" role="status"><div><strong>This period has missing coverage.</strong><p>Totals and charts are unavailable. You can still inspect recorded transactions. Choose a covered period or import the missing statements.</p></div></div>}
   <div aria-busy={loading} className={loading?'content-loading':''}>
   {tab==='Overview'&&<>
    <OverviewView data={data} start={data.start} end={data.end} initialPocketCurrency={initialUrlPocketCurrency} onOpen={setTab} onForecast={setForecastPage}/>
    <div className="insight-grid"><button className="insight" onClick={()=>setTab('Planning')}><span className="insight-icon"><RefreshCw size={18}/></span><div><h3>What keeps repeating?</h3><p>{data.recurring.length?`${data.recurring.length} recurring payment candidates to inspect in Planning.`:'Inspect commitments in the Planning editor.'}</p></div><ArrowRight size={17}/></button><button className="insight" onClick={()=>drill('','','review')}><span className="insight-icon sand"><CircleHelp size={18}/></span><div><h3>What needs a second look?</h3><p>{reviewCount?`${reviewCount} transactions need categorization or review.`:'Your eligible transactions are categorized.'}</p></div><ArrowRight size={17}/></button><ForecastEstimateSummary page={forecastPage??null} onOpen={()=>setTab('Forecast')}/></div>
   </>}
   {(tab==='Analysis'||tab==='Cash Flow')&&<AnalysisView start={data.start} end={data.end} accounts={data.accounts} categories={data.categories} currencies={data.currencies} initialPocketCurrency={initialUrlPocketCurrency} onOpenTransactions={(cat,merchant)=>drill(cat,merchant,(cat||merchant)?'spending':'')} onOpenTransactionAnchor={openReportedAnchor} onOpenDebts={()=>setTab('Debts')}/>}
   {tab==='Categorization'&&<CategorizationView generation={data.generation} freshness={data.freshness} categories={data.categories} accounts={data.accounts} currencies={data.currencies} onSaved={saved} onOpenRules={()=>setTab('Rules')} onOpenTransactions={()=>drill('','','')} onOpenNext={setTab}/>}
   {tab==='Income'&&<InflowsView generation={data.generation} freshness={data.freshness} categories={data.categories} onOpenTransactions={()=>drill('','','pending_review')} onSaved={saved}/>}
   {tab==='Trips'&&<TripsView generation={data.generation} onSaved={saved}/>}
   {tab==='Reports'&&<ReportsView generation={data.generation} onSaved={saved} categories={data.categories} accounts={data.accounts} currencies={data.currencies}/>}
   {tab==='Debts'&&<><div className="explanation"><Wallet size={18}/><p>Informal money you owe or are owed. Payables and receivables are shown separately and never netted. Record borrowing, lending, repayments and shared-expense splits against the bank transactions that funded them; principal never counts as spending or income.</p></div><DebtsView generation={data.generation} categories={data.categories} onSaved={saved}/></>}
   {tab==='Cards'&&<><div className="explanation"><CreditCard size={18}/><p>Credit cards and their observed statements. Each figure is a fact at a statement period end using that issuer's verified source equation — never a timeless “current balance”. Missing statement periods and reconciliation status are shown before the picture is called complete.</p></div><CardsView generation={data.generation} onSaved={saved}/></>}
   {tab==='Installments'&&<><div className="explanation"><Layers3 size={18}/><p>Confirmed source-proven installment chains on your cards. Known and unknown principal are both shown; an unknown principal infers no remaining liability. The projected schedule stays distinct from the actual observed occurrences.</p></div><InstallmentsView generation={data.generation} onSaved={saved}/></>}
   {(tab==='Obligations'||tab==='Money Owed')&&<><div className="explanation"><Layers3 size={18}/><p>Debts, installment plans, card statement dues and recurring commitments merged behind one shape, without flattening what makes them different. Rows are grouped by confidence; an unresolved cash-settlement requirement is shown as a named gap, never as a zero. Read-only.</p></div><ObligationsView/></>}
   {tab==='Net worth'&&<><div className="explanation"><Wallet size={18}/><p>A per-currency subtotal of the dated cash, debt, receivable and card positions the ledger can already support — each figure from exactly one upstream read, so nothing is counted twice. Components it cannot value are listed as missing, never as zero. Manual values keep their basis on a separate line. It is a tracked subtotal, not a statement of total personal wealth. Read-only.</p></div><NetWorthView/></>}
   {tab==='Transactions'&&<TransactionsView start={data.start} end={data.end} generation={data.generation} freshness={data.freshness} categories={data.categories} accounts={data.accounts} currencies={data.currencies} initialPocketCurrency={initialUrlPocketCurrency} initialCategory={category} initialSearch={search} initialKind={kind} onOpenEntry={row=>{setEntry(row.entry);setReportedRow(row)}} onSaved={saved}/>}
   {tab==='Review'&&legacyData&&<><WorkspaceReportingDetails data={legacyData}/><ReviewHub freshness={data.freshness} onSaved={saved} onOpenCategorization={()=>setTab('Categorization')}/><div className="explanation"><ShieldCheck size={18}/><p>These pairs were matched automatically by description, amount and date across two accounts, but were not confidently unique. Confirming records both legs as one internal transfer, so they leave spending and income. Rejecting keeps them as ordinary transactions. Currency exchanges and manual links use the CLI.</p></div><Card className="recurring-card"><PanelTitle eyebrow="PROPOSED TRANSFERS" title={`${legacyData.transfer_proposals.length} pair${legacyData.transfer_proposals.length===1?'':'s'} awaiting a decision`}/>{legacyData.transfer_proposals.map(p=><ProposalRow key={p.id} proposal={p} data={data} onSaved={saved}/>)}{!legacyData.transfer_proposals.length&&<Empty title="Nothing to review" detail="Automatic transfer proposals appear here after an import or refresh. Confirmed pairs are already excluded; rejected ones stay as they are."/>}</Card><RelationshipEditor generation={data.generation} freshness={data.freshness} onSaved={saved}/><p className="muted-note">Deciding here appends one durable decision to your ledger and refreshes totals. A decision can be reversed later with “Undo a decision” above. Proposals are per generation; refresh if the ledger changed.</p></>}
   {tab==='Recurring'&&legacyData&&<><WorkspaceReportingDetails data={legacyData}/><div className="explanation"><RefreshCw size={18}/><p>Three or more similar payments, 6–8 or 25–35 days apart, with amounts within 10%. These are candidates, not confirmed subscriptions. Predictions use the latest payment and may be overdue.</p></div><Card className="recurring-card"><PanelTitle eyebrow="DETECTED PATTERNS" title={`${legacyData.recurring.length} recurring candidates`}/>{legacyData.recurring.map(r=><button className="recurring-row" key={r.merchant} onClick={()=>{setStart(data.trend[0].month+'-01');void load(data.trend[0].month+'-01',data.end);drill('',r.merchant,'spending')}}><span className="merchant-icon"><CreditCard size={18}/></span><span className="recent-name"><strong>{r.merchant}</strong><small>{r.cadence} · {r.count} observed payments · last {dateLabel(r.last_date)}</small><small>Estimated next: {dateLabel(r.next_date)} · verify with provider</small></span><strong>{reportedMoney(r, 'amount', (r as typeof r & {currency:string}).currency)}</strong><ArrowRight size={16}/></button>)}{!legacyData.recurring.length&&<Empty title="No regular patterns yet" detail="Import at least three months of statements to help identify repeating payments. Some subscriptions vary too much to detect automatically."/>}</Card><p className="muted-note">Detection examines covered history up to the selected end date in {currency}. A recurring charge can be rent, a transfer-like payment, or a subscription; inspect its source before making a decision.</p></>}
   {tab==='Plans'&&legacyData&&<><WorkspaceReportingDetails data={legacyData}/><PanelTitle eyebrow="MONTHLY BUDGETS" title="Give your spending some boundaries"/>{!legacyData.plans.some(p=>p.type==='budget')?<Card><Empty title="Start with one useful limit" detail="Choose a monthly amount for a category or all spending. Compare it with a complete covered calendar month." action={<Button onClick={()=>setDialog('budget')}><Plus size={16}/> Create a budget</Button>}/></Card>:<div className="plan-grid">{legacyData.plans.filter(p=>p.type==='budget').map(p=><PlanCard key={p.id} plan={p} edit={()=>{setEditingPlan(p);setDialog('budget')}}/>)}</div>}<p className="muted-note">Budgets are monthly limits, not forecasts. Category budgets include gross purchases; an all-spending budget also includes fees. Overlapping budgets are not added together.</p><div className="section-heading"><PanelTitle eyebrow="SAVINGS GOALS" title="A little closer to what’s next"/><Button variant="outline" onClick={()=>{setEditingPlan(null);setDialog('goal')}}><Plus size={16}/> New goal</Button></div>{!legacyData.plans.some(p=>p.type==='goal')?<Card><Empty title="What are you saving for?" detail="An emergency fund, a trip, or something bigger. Record your target and current saved amount to track progress." action={<Button variant="outline" onClick={()=>setDialog('goal')}>Create a savings goal</Button>}/></Card>:<div className="plan-grid">{legacyData.plans.filter(p=>p.type==='goal').map(p=><PlanCard key={p.id} plan={p} edit={()=>{setEditingPlan(p);setDialog('goal')}}/>)}</div>}<p className="muted-note">Goal progress is manually recorded. It does not move money or infer balances from income and spending.</p></>}
   {tab==='Categories'&&<><div className="explanation"><Layers3 size={18}/><p>Categories describe what money was for. Changing a category keeps the original transaction and its cash-flow classification intact. Custom categories are saved in the ledger’s decision history.</p></div><CustomCategoryManager freshness={data.freshness} onSaved={saved}/><div className="category-grid">{Array.from(new Set(data.categories.map(c=>c.split(' / ')[0]))).map((group,i)=><Card key={group} className="category-group"><div className="category-group-header"><span className="group-icon" style={categoryIconColor(i)}><Layers3 size={18}/></span><h2>{group}</h2></div>{data.categories.filter(c=>c.startsWith(group+' / ')).map(c=><button key={c} onClick={()=>drill(c)}><span>{shortCategory(c)}</span>{data.custom_categories.includes(c)&&<Badge variant="outline">Custom</Badge>}<ChevronRight size={13}/></button>)}</Card>)}</div></>}
   {tab==='Rules'&&<RulesView generation={data.generation} freshness={data.freshness} categories={data.categories} onSaved={saved}/>}
   {tab==='Planning'&&<PlanningView generation={data.generation} freshness={data.freshness} categories={data.categories} onSaved={saved}/>}
   {tab==='Baseline'&&<BaselineView onOpenTransactionAnchor={openReportedAnchor}/>}
   {tab==='Cash'&&<CashView generation={data.generation} onSaved={saved} onOpenSetup={()=>setTab('Setup')}/>}
   {tab==='Forecast'&&<ForecastView/>}
   {tab==='Controls'&&<AllControlsView initialMonth={(end || new Date().toISOString().slice(0, 7)).slice(0, 7)}/>}
   {tab==='Will I have enough'&&<SufficiencyView generation={data.generation} onSaved={saved} onOpen={setTab} onOpenObligations={()=>setTab('Obligations')} onOpenCash={()=>setTab('Cash')}/>}
   {tab==='Annual funds'&&<FundsView onSaved={saved}/>}
   {tab==='Variance'&&<VarianceView generation={data.generation} currency="SGD" currencies={data.currencies} onSaved={saved}/>}
   {tab==='Maintenance'&&<MaintenanceView/>}
   {tab==='Accounts'&&<AccountsView freshness={data.freshness} onSaved={saved}/>}
   {tab==='Setup'&&<SetupView generation={data.generation} onImported={()=>{void load()}} onReview={()=>{setTab('Categorization')}}/>}
   </div>
   <footer><span><ShieldCheck size={13}/> On your device. Backed by your records.</span><span>Refreshed {new Date(data.updated_at).toLocaleString('en-SG',{dateStyle:'medium',timeStyle:'short'})}</span></footer>
   {historyRows&&<Dialog open onOpenChange={open=>{if(!open)setHistoryRows(null)}}><DialogContent><DialogHeader><DialogTitle>Recent decisions</DialogTitle><DialogDescription>The latest 50 durable changes in your ledger, including category changes and plans.</DialogDescription></DialogHeader><div className="history-list">{historyRows.map(r=><article key={r.sequence}><span className="eyebrow">#{r.sequence} · {new Date(r.date).toLocaleString('en-SG')}</span><h3>{r.kind.toLowerCase().replaceAll('_',' ')}</h3>{r.category&&<p>{r.category}</p>}<small>{r.reason}</small></article>)}{!historyRows.length&&<p>No decisions recorded yet.</p>}</div></DialogContent></Dialog>}
   {entry&&<EntryDialog key={entry.anchor} entry={entry} data={data} onClose={()=>{setEntry(null);setReportedRow(null)}} onSaved={saved} onRelationshipSaved={async s=>{setNotice(s==='recorded_refresh_pending'?'Saved; totals awaiting refresh. Use Refresh to retry.':'Saved to your ledger.');await load()}} statementDetail={reportedRow??undefined}/>}
   {dialog&&<CreateDialog type={dialog} plan={editingPlan} data={data} onClose={()=>{setDialog(null);setEditingPlan(null)}} onSaved={saved}/>}
   </>}
  </main>
  </div>
  <CommandPalette open={paletteOpen} onClose={()=>setPaletteOpen(false)} onSelect={goTo}/>
 </div>
}

function PanelTitle({eyebrow,title,action}:{eyebrow?:string;title:string;action?:React.ReactNode}){return <div className="panel-title"><div>{eyebrow&&<p className="eyebrow">{eyebrow}</p>}<h2>{title}</h2></div>{action}</div>}
function Empty({title,detail,action}:{title:string;detail:string;action?:React.ReactNode}){return <div className="empty"><BarChart3 size={25}/><h3>{title}</h3><p>{detail}</p>{action}</div>}
function PlanCard({plan:p,edit}:{plan:Plan;edit:()=>void}){return <Card className="plan-card"><div className="plan-title"><span className="insight-icon">{p.type==='goal'?<Target size={18}/>:<Wallet size={18}/>}</span><Button variant="ghost" size="sm" onClick={edit}>Edit</Button></div><h3>{p.name}</h3><p>{p.type==='goal'?'Manually recorded savings':p.category?shortCategory(p.category):'All spending + fees'} · {p.currency}</p><div className="plan-values"><strong>{reportedMoney(p, 'actual', p.currency)}</strong><span>of {reportedMoney(p, 'target', p.currency)}</span></div><div className="progress-track" role="progressbar" aria-label={`${p.name} progress`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(pct(p.actual,p.target))}><span style={{width:`${pct(p.actual,p.target)}%`}}/></div><small>{p.actual===null?'Choose one complete covered calendar month in this currency.':Number(p.remaining)<0?`${reportedMoney(p, 'remaining', p.currency).replace(/^[-−]/, '')} ${p.type==='budget'?'over budget':'above target'}`:`${reportedMoney(p, 'remaining', p.currency)} ${p.type==='budget'?'left to spend':'left to save'}`}</small></Card>}

function ProposalRow({proposal:p,data,onSaved}:{proposal:Proposal;data:Dashboard;onSaved:(s:string)=>Promise<void>}){
 const [decision,setDecision]=useState<'confirm'|'reject'|null>(null),[reason,setReason]=useState('')
 const cmd=useCommand(data,onSaved)
 function choose(d:'confirm'|'reject'){setDecision(d);cmd.reset()}
 return <div className="proposal">
  <div className="proposal-legs">
   {[{a:p.left_account,d:p.left_date,t:p.left_description,m:p.left_amount,c:p.left_currency},{a:p.right_account,d:p.right_date,t:p.right_description,m:p.right_amount,c:p.right_currency}].map((leg,i)=><div className="proposal-leg" key={i}><span className="merchant-icon"><Wallet size={16}/></span><div><strong>{leg.a}</strong><small>{dateLabel(leg.d)}{leg.t?` · ${leg.t}`:''}</small></div><strong>{reportedMoney(p, i===0?'left_amount':'right_amount', leg.c)}</strong></div>)}
  </div>
  <p className="proposal-basis"><Badge variant="outline">{p.link_kind==='CURRENCY_EXCHANGE'?'Currency exchange':'Same-currency transfer'}</Badge> {p.basis}</p>
  {!decision?<div className="dialog-actions"><Button disabled={data.freshness==='stale'} onClick={()=>choose('confirm')}><Check size={15}/> Confirm transfer</Button><Button variant="outline" disabled={data.freshness==='stale'} onClick={()=>choose('reject')}><X size={15}/> Not a transfer</Button></div>:
  <form onChange={cmd.reset} onSubmit={e=>{e.preventDefault();void cmd.act({kind:'TRANSFER_DECISION',anchors:{left:p.left,right:p.right},payload:{decision,reason}})}}>
   <label>Reason<Input required maxLength={2000} disabled={cmd.attempted} placeholder={decision==='confirm'?'Why are these the same transfer?':'Why is this not an internal transfer?'} value={reason} onChange={e=>setReason(e.target.value)}/></label>
   <CommandActions cmd={cmd} disabled={data.freshness==='stale'} description={decision==='confirm'?'Both legs become an internal transfer and leave spending and income.':'The pair stays as two ordinary transactions.'}/>
   {!cmd.attempted&&<Button type="button" variant="ghost" size="sm" onClick={()=>{setDecision(null);setReason('')}}>Cancel</Button>}
  </form>}
 </div>
}
function useCommand(data:Dashboard,onSaved:(s:string)=>Promise<void>){
 const [error,setError]=useState(''),[busy,setBusy]=useState(false),[preview,setPreview]=useState<Command|null>(null),[attempted,setAttempted]=useState(false)
 const inFlight=useRef(false)
 function reset(){if(!attempted)setPreview(null)}
 async function act(command:Omit<Command,'request_id'|'expected_generation'>){if(inFlight.current)return;inFlight.current=true;setError('');setBusy(true);try{if(!preview){const c={...command,request_id:crypto.randomUUID(),expected_generation:data.generation};await api('preview',c);setPreview(c)}else{setAttempted(true);const result=await api<{status:string}>('commands',preview);await onSaved(result.status)}}catch(e){setError((e as Error).message);if(e instanceof ApiError&&[409,422].includes(e.status)){setAttempted(false);setPreview(null)}}finally{inFlight.current=false;setBusy(false)}}
 return {error,busy,preview,attempted,reset,act}
}
function CreateDialog({type,plan,data,onClose,onSaved}:{type:'category'|'budget'|'goal';plan:Plan|null;data:Dashboard;onClose:()=>void;onSaved:(s:string)=>Promise<void>}){
 const [name,setName]=useState(plan?.name||''),[group,setGroup]=useState('Lifestyle'),[category,setCategory]=useState(plan?.category||''),[target,setTarget]=useState(plan?.target||''),[saved,setSaved]=useState(plan?.saved||'0'),[currency,setCurrency]=useState(plan?.currency||data.currency),[reason,setReason]=useState('')
 const cmd=useCommand(data,onSaved),id=useRef(plan?.id||crypto.randomUUID())
 function close(){if(!cmd.busy&&(!name&&!reason||confirm('Discard this unsaved draft?')))onClose()}
 return <Dialog open onOpenChange={open=>{if(!open)close()}}><DialogContent><DialogHeader><DialogTitle>{plan?'Edit':'Create'} {type==='category'?'a category':type==='budget'?'a monthly budget':'a savings goal'}</DialogTitle><DialogDescription>{type==='category'?'Make your categories match your life.':type==='budget'?'A reusable monthly limit, compared with covered ledger spending.':'Set a target and explicitly record how much you have saved.'}</DialogDescription></DialogHeader><form onChange={cmd.reset} onSubmit={e=>{e.preventDefault();void cmd.act(type==='category'?{kind:'CATEGORY_CREATE',anchors:{category:`${group.trim()} / ${name.trim()}`},payload:{category:`${group.trim()} / ${name.trim()}`,reason}}:{kind:'PLAN_SET',anchors:{plan:id.current},payload:{type,name:name.trim(),category,currency,target,saved,reason}})}}><fieldset disabled={cmd.attempted}><label>{type==='category'?'Category name':'Name'}<Input required value={name} maxLength={80} placeholder={type==='goal'?'Emergency fund':type==='budget'?'Monthly dining':'Pets & care'} onChange={e=>setName(e.target.value)}/></label>{type==='category'?<label>Group<Input list="category-groups" required maxLength={80} value={group} onChange={e=>setGroup(e.target.value)}/><datalist id="category-groups">{Array.from(new Set(data.categories.map(c=>c.split(' / ')[0]))).filter(g=>!['Internal','Unclassified'].includes(g)).map(g=><option key={g}>{g}</option>)}</datalist><small>Choose an existing group or enter a new one.</small></label>:<>{type==='budget'&&<label>Category<select value={category} onChange={e=>setCategory(e.target.value)}><option value="">All spending + fees</option>{data.categories.filter(c=>!c.startsWith('Internal')).map(c=><option key={c}>{c}</option>)}</select></label>}<div className="form-row"><label>{type==='goal'?'Target amount':'Monthly limit'}<Input required inputMode="decimal" pattern="[0-9]+(\.[0-9]{1,2})?" placeholder="0.00" value={target} onChange={e=>setTarget(e.target.value)}/></label><label>Currency<select value={currency} onChange={e=>setCurrency(e.target.value)}>{data.currencies.map(c=><option key={c}>{c}</option>)}</select></label></div>{type==='goal'&&<label>Amount currently saved<Input required inputMode="decimal" pattern="[0-9]+(\.[0-9]{1,2})?" value={saved} onChange={e=>setSaved(e.target.value)}/><small>Manually recorded; no money is moved.</small></label>}</>}<label>Reason<Input required maxLength={2000} placeholder="A short note for your decision history" value={reason} onChange={e=>setReason(e.target.value)}/></label></fieldset><CommandActions cmd={cmd} disabled={data.freshness==='stale'} description={type==='category'?`Create ${group} / ${name}`:`${name}: ${money(target,currency)} ${type==='budget'?'per month':`target, ${money(saved,currency)} saved`}`}/></form></DialogContent></Dialog>
}
function CommandActions({cmd,description,disabled}:{cmd:ReturnType<typeof useCommand>;description:string;disabled:boolean}){return <>{cmd.error&&<p className="form-error" role="alert">{cmd.error}</p>}{cmd.preview&&<div className="preview" role="status"><Check size={16}/><div><strong>Ready to save</strong><p>{description}</p></div></div>}{cmd.attempted&&cmd.error&&<p className="muted-note">The outcome may be unknown. Retry sends the same request safely; do not create a second decision.</p>}{disabled&&<p className="form-error">Refresh the ledger before editing.</p>}<div className="dialog-actions"><Button type="submit" disabled={cmd.busy||disabled}>{cmd.busy?'Working…':cmd.preview?(cmd.attempted?'Retry same save':'Save to ledger'):'Preview change'}{!cmd.busy&&<ArrowRight size={15}/>}</Button></div></>}
