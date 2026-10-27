import OutsidePrintBinding from './OutsidePrintBinding'
import ProductionHistory from './ProductionHistory'
import { useEffect, useRef, useState } from 'react'
import { production, ProductionState, ProductionJob, OutsideProductionJob, RecipeOutput } from '../../api/production'
import { square } from '../../api/square'
import { getCurrentUser } from '../../permissions'

export default function ProductionBatches({products,inspectJobId}:{products:{id:number;name:string}[];inspectJobId?:number}) {
  const [open,setOpen]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState('')
  const [state,setState]=useState<ProductionState|null>(null),[jobs,setJobs]=useState<(ProductionJob|OutsideProductionJob)[]>([])
  const [jobOffset,setJobOffset]=useState<number|null>(null)
  const [showRecipeEditor,setShowRecipeEditor]=useState(false)
  const [name,setName]=useState(''),[materials,setMaterials]=useState(''),[reference,setReference]=useState('')
  const [qualified,setQualified]=useState(false),[outputs,setOutputs]=useState<RecipeOutput[]>([{product_id:0,kind:'main',expected:1}])
  const [recipeId,setRecipeId]=useState(''),[job,setJob]=useState(''),[confirmed,setConfirmed]=useState(false)
  const [counts,setCounts]=useState<{accepted:string;rejected:string;baseline_disposition?:'in_baseline'|'send'}[]>([]),[notice,setNotice]=useState('')
  const [historyRevision,setHistoryRevision]=useState(0)
  const generation=useRef(0)
  const role=getCurrentUser()?.role,admin=role==='admin',operator=role==='admin'||role==='operator',recipe=state?.recipes.find(r=>r.id===recipeId)
  const run=async(fn:()=>Promise<void>)=>{setBusy(true);setError('');try{await fn()}catch(e){setError(e instanceof Error?e.message:'Could not complete the request. Reload and try again.')}finally{setBusy(false)}}
  const load=async()=>{
    const g=++generation.current
    const [s,j]=await Promise.all([production.state(),operator?production.jobs(0,inspectJobId):Promise.resolve({jobs:[],next_offset:null})])
    if(inspectJobId){
      while(s.next_offset!==null){const page=await production.state(s.next_offset);s.recipes.push(...page.recipes);s.next_offset=page.next_offset}
    }
    if(g===generation.current){
      if(inspectJobId){
        const target=j.jobs.find(v=>!v.kind&&v.id===inspectJobId)
        if(!target)throw new Error('Inspection unavailable for this job.')
        setJob(String(inspectJobId))
        const matches=s.recipes.filter(r=>r.qualified&&r.payload&&Object.keys(r.payload).every(k=>r.payload![k]===(target.payload as Record<string,unknown>)[k]))
        if(matches.length===1){setRecipeId(matches[0].id);setCounts(matches[0].outputs.map(()=>({accepted:'',rejected:''})))}
        else setError(matches.length?'Recipe ambiguous. Review the recipe revisions.':'No qualified recipe for this job.')
      }
      setState(s);setJobs(j.jobs);setJobOffset(j.next_offset);setHistoryRevision(old=>old+1)}
  }
  useEffect(()=>{if(inspectJobId){setOpen(true);void run(load)}},[inspectJobId])
  const toggle=()=>{const next=!open;setOpen(next);if(next)void run(load)}
  const selectRecipe=(id:string)=>{setRecipeId(id);setConfirmed(false);setNotice('');setCounts((state?.recipes.find(r=>r.id===id)?.outputs??[]).map(()=>({accepted:'',rejected:''})))}
  const readyCounts=!!recipe&&counts.length===recipe.outputs.length&&counts.every((c,i)=>c.accepted!==''&&c.rejected!==''&&Number.isInteger(Number(c.accepted))&&Number.isInteger(Number(c.rejected))&&Number(c.accepted)>=0&&Number(c.rejected)>=0&&Number(c.accepted)+Number(c.rejected)===recipe.outputs[i].expected)
  const input='w-full min-w-0 rounded-md px-3 py-2 bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)]'
  const button='px-3 py-2 rounded-md border border-[var(--brand-card-border)] disabled:opacity-50 text-sm'
  return <section className="mb-6 rounded-md border border-[var(--brand-card-border)] bg-[var(--brand-card-bg)] p-4">
    <button type="button" aria-expanded={open} className="font-medium text-[var(--brand-primary)]" disabled={busy} onClick={toggle}>Recipes and finished batches</button>
    {open&&<div className="mt-3 space-y-4" aria-busy={busy}>
      <p className="text-sm text-[var(--brand-muted)]">A completed print still needs inspection. Record sellable main and flush pieces separately. Square stock additions are prepared locally. Send them separately after reviewing the saved destination.</p>
      {busy&&<p role="status">Working…</p>}{error&&<p role="alert" className="text-red-400 break-words">{error}</p>}{notice&&<p role="status">{notice}</p>}
      <button className={button} disabled={busy} onClick={()=>void run(load)}>Reload batches</button>
      {operator&&<div className="space-y-2"><p className="text-sm">Only completed single-attempt jobs with retained physical print evidence appear. Legacy, retried and ambiguous jobs need separate review.</p>
        {jobs.length===0&&<p>No eligible completed print jobs yet. Nothing needs to be entered while you are away from the printer.</p>}
        {jobOffset!==null&&<button className={button} disabled={busy} onClick={()=>void run(async()=>{const page=await production.jobs(jobOffset);setJobs(old=>[...old,...page.jobs]);setJobOffset(page.next_offset)})}>Load older completed jobs</button>}
      </div>}
      {operator&&state&&<OutsidePrintBinding recipes={state.recipes} products={products} admin={admin} onBound={load}/>}
      {admin&&<button className={button} disabled={busy} type="button" aria-expanded={showRecipeEditor} onClick={()=>setShowRecipeEditor(old=>!old)}>Create recipe revision</button>}
      {admin&&showRecipeEditor&&<form className="space-y-3 border-t border-[var(--brand-card-border)] pt-3" onSubmit={e=>{e.preventDefault();void run(async()=>{
        const saved=await production.recipe({reference_job_id:Number(reference),name,materials,qualification_confirmed:qualified,outputs})
        await load();setNotice(`${saved.qualified?'Qualified recipe':'Draft recipe'} saved. Changing it requires a new revision.`);setQualified(false)
      })}}>
        <h3 className="font-medium">Create a recipe revision</h3>
        <label className="block text-sm">Recipe name<input className={input} value={name} maxLength={160} disabled={busy} onChange={e=>setName(e.target.value)} required/></label>
        <label className="block text-sm">Successful reference job<select aria-label="Successful reference job" className={input} value={reference} disabled={busy} onChange={e=>{setReference(e.target.value);setQualified(false)}} required><option value="">Choose completed job</option>{jobs.filter((v):v is ProductionJob=>!v.kind).map(j=><option value={j.id} key={j.id}>{j.name} · job {j.id} · plate {j.payload.plate_index+1}</option>)}</select></label>
        <label className="block text-sm">Material and color requirements<textarea className={input} value={materials} maxLength={1000} disabled={busy} onChange={e=>{setQualified(false);setMaterials(e.target.value)}} placeholder="Material, colors or allowed alternatives, nozzle and finishing requirements" required/></label>
        <p className="text-sm">Fixed output per plate, including rejected pieces. Use this flow for finished single-print products. Assembly-dependent products need their own process.</p>
        {outputs.map((o,i)=><fieldset className="space-y-2 rounded-md border border-[var(--brand-card-border)] p-3" key={i}><legend>Output {i+1}</legend>
          <label className="block text-sm">Product<select aria-label={`Output ${i+1} product`} className={input} value={o.product_id||''} disabled={busy} required onChange={e=>{setQualified(false);setOutputs(old=>old.map((v,n)=>n===i?{...v,product_id:Number(e.target.value)}:v))}}><option value="">Choose product</option>{products.map(p=><option value={p.id} key={p.id}>{p.name}</option>)}</select></label>
          <label className="block text-sm">Output type<select aria-label={`Output ${i+1} type`} className={input} value={o.kind} disabled={busy} onChange={e=>{setQualified(false);setOutputs(old=>old.map((v,n)=>n===i?{...v,kind:e.target.value as 'main'|'flush'}:v))}}><option value="main">Main product</option><option value="flush">Sellable flush product</option></select></label>
          <label className="block text-sm">Expected pieces<input aria-label={`Output ${i+1} expected pieces`} type="number" min="1" max="10000" step="1" required className={input} disabled={busy} value={o.expected} onChange={e=>{setQualified(false);setOutputs(old=>old.map((v,n)=>n===i?{...v,expected:Number(e.target.value)}:v))}}/></label>
          {outputs.length>1&&<button type="button" className={button} disabled={busy} onClick={()=>{setQualified(false);setOutputs(old=>old.filter((_,n)=>n!==i))}}>Remove output {i+1}</button>}
        </fieldset>)}
        <button type="button" className={button} disabled={busy||outputs.length>=8} onClick={()=>{setQualified(false);setOutputs(old=>[...old,{product_id:0,kind:'flush',expected:1}])}}>Add output</button>
        <label className="flex items-start gap-2 text-sm"><input type="checkbox" checked={qualified} disabled={busy} onChange={e=>setQualified(e.target.checked)}/>I verified this reference print, material/color setup and fixed yield. These outputs are ready for inspection as finished products.</label>
        <button className={button} type="submit" disabled={busy||!reference||!name.trim()||!materials.trim()||outputs.some(o=>!o.product_id||!Number.isInteger(o.expected)||o.expected<1)}>Save recipe revision</button>
      </form>}
      {state&&<div className="space-y-2"><h3 className="font-medium">Recipe revisions</h3>{state.recipes.length===0&&<p>No recipes yet.</p>}{state.recipes.map(r=><div key={r.id} className="text-sm break-words">{r.name} · {r.qualified?'Qualified':'Draft'} · {r.outputs.map(o=>`${o.expected} ${o.product_name} (${o.kind})`).join(', ')}<p className="text-[var(--brand-muted)]">{r.materials}</p>{r.qualified_from_outside_print&&<p>Qualified from owner-attested outside print {r.outside_print_evidence?.print_job_id}.</p>}{admin&&<button className={button} disabled={busy} type="button" onClick={()=>{setName(r.name);setMaterials(r.materials);setReference(r.reference_job_id===null?'':String(r.reference_job_id));setOutputs(r.outputs.map(o=>({product_id:o.product_id,kind:o.kind,expected:o.expected})));setQualified(false);setShowRecipeEditor(true)}}>Use as new revision</button>}</div>)}
        {state.next_offset!==null&&<button className={button} disabled={busy} onClick={()=>void run(async()=>{const page=await production.state(state.next_offset!);setState(old=>old?{...old,recipes:[...old.recipes,...page.recipes],next_offset:page.next_offset}:page)})}>Load more recipes</button>}
      </div>}
      {operator&&<form className="space-y-3 border-t border-[var(--brand-card-border)] pt-3" onSubmit={e=>{e.preventDefault();if(!recipe)return;void run(async()=>{
        const revision=counts.some(c=>Number(c.accepted)>0)?(await square.status()).revision:null
        const saved=await production.inspect({recipe_id:recipe.id,...(job.startsWith('outside:')?{print_job_id:Number(job.slice(8))}:{job_id:Number(job)}),square_revision:revision,inspection_confirmed:confirmed,
          outputs:recipe.outputs.map((o,i)=>({product_id:o.product_id,kind:o.kind,accepted:Number(counts[i].accepted),rejected:Number(counts[i].rejected),...(counts[i].baseline_disposition?{baseline_disposition:counts[i].baseline_disposition}:{})}))})
        await load();setConfirmed(false);setNotice(`Inspection recorded for ${saved.print_job_id?`outside print ${saved.print_job_id}`:`job ${saved.job_id}`}. ${saved.stock_status==='prepared'?'Square addition prepared, not sent.':saved.stock_status==='in_baseline'?'Accepted output counted in baseline; no Square addition.':'No sellable pieces added.'}`)
      })}}>
        <h3 className="font-medium">Inspect a finished batch</h3>
        <label className="block text-sm">Qualified recipe<select aria-label="Qualified recipe" className={input} value={recipeId} disabled={busy} required onChange={e=>selectRecipe(e.target.value)}><option value="">Choose recipe</option>{state?.recipes.filter(r=>r.qualified).map(r=><option key={r.id} value={r.id}>{r.name} · {r.id.slice(0,8)}</option>)}</select></label>
        <label className="block text-sm">Completed batch job<select aria-label="Completed batch job" className={input} value={job} disabled={busy} required onChange={e=>{setJob(e.target.value);setConfirmed(false)}}><option value="">Choose completed job</option>{jobs.map(j=>'print_job_id' in j?<option key={`outside:${j.print_job_id}`} value={`outside:${j.print_job_id}`}>{j.name} · owner-attested outside print {j.print_job_id}</option>:<option key={j.id} value={j.id}>{j.name} · job {j.id}</option>)}</select></label>
        {recipe?.outputs.map((o,i)=><fieldset key={`${o.product_id}-${o.kind}`} className="space-y-2 rounded-md border border-[var(--brand-card-border)] p-3"><legend className="text-sm">{o.product_name} · {o.kind} · expected {o.expected}</legend>
          {(['accepted','rejected'] as const).map(kind=><label key={kind} className="block text-sm">{kind==='accepted'?'Sellable pieces':'Rejected pieces'}<input aria-label={`${o.product_name} ${o.kind} ${kind}`} className={input} type="number" min="0" max={o.expected} step="1" required disabled={busy} value={counts[i]?.[kind]??''} onChange={e=>{setConfirmed(false);setCounts(old=>old.map((v,n)=>n===i?{...v,[kind]:e.target.value}:v))}}/></label>)}
          <label className="block text-sm">Baseline membership (required for older output with a baseline)<select aria-label={`${o.product_name} ${o.kind} baseline membership`} className={input} disabled={busy} value={counts[i]?.baseline_disposition??''} onChange={e=>{setConfirmed(false);setCounts(old=>old.map((v,n)=>n===i?{...v,baseline_disposition:(e.target.value||undefined) as 'in_baseline'|'send'|undefined}:v))}}><option value="">Choose if required</option><option value="in_baseline">Counted in baseline</option><option value="send">Not counted, send</option></select></label>
        </fieldset>)}
        <label className="flex items-start gap-2 text-sm"><input type="checkbox" checked={confirmed} disabled={busy} onChange={e=>setConfirmed(e.target.checked)}/>I inspected this physical batch. Accepted pieces are sellable and counts include any display pieces.</label>
        <button className={button} type="submit" disabled={busy||!recipe?.qualified||!job||!confirmed||!readyCounts}>Record inspection</button>
      </form>}
      <ProductionHistory operator={operator} refreshKey={historyRevision}/>
    </div>}
  </section>
}
