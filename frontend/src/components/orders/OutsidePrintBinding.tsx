import { useState } from 'react'
import { printJobs } from '../../api/jobs'
import type { PrintJob } from '../../types'
import { production, Recipe, RecipeOutput } from '../../api/production'

export default function OutsidePrintBinding({recipes,products,admin,onBound}:{
  recipes:Recipe[];products:{id:number;name:string}[];admin:boolean;onBound:()=>Promise<void>
}) {
  const [open,setOpen]=useState(false),[qualify,setQualify]=useState(false),[busy,setBusy]=useState(false)
  const [printId,setPrintId]=useState(''),[recipeId,setRecipeId]=useState(''),[printerId,setPrinterId]=useState('')
  const [fileId,setFileId]=useState(''),[plate,setPlate]=useState('1'),[name,setName]=useState(''),[materials,setMaterials]=useState('')
  const [outside,setOutside]=useState(false),[fileConfirmed,setFileConfirmed]=useState(false),[yieldConfirmed,setYieldConfirmed]=useState(false)
  const [outputs,setOutputs]=useState<RecipeOutput[]>([{product_id:0,kind:'main',expected:1}])
  const [recent,setRecent]=useState<PrintJob[]|null>(null)
  const [error,setError]=useState(''),[notice,setNotice]=useState('')
  const input='block w-full rounded-md border border-[var(--brand-card-border)] bg-[var(--brand-content-bg)] px-3 py-2'
  const button='rounded-md border border-[var(--brand-card-border)] px-3 py-2 disabled:opacity-50'
  const valid=outside&&fileConfirmed&&Number(printId)>0&&(qualify?
    Number(printerId)>0&&Number(plate)>0&&name.trim()&&materials.trim()&&yieldConfirmed&&outputs.every(o=>o.product_id>0&&Number.isInteger(o.expected)&&o.expected>0):!!recipeId)
  const resetConfirmations=()=>{setOutside(false);setFileConfirmed(false);setYieldConfirmed(false);setNotice('')}
  return <div>
    <button type="button" className={button} aria-expanded={open} disabled={busy} onClick={()=>setOpen(!open)}>Bind an outside print</button>
    {open&&<form className="mt-3 space-y-3" aria-busy={busy} onSubmit={async e=>{
      e.preventDefault();if(!valid||busy)return
      setBusy(true);setError('');setNotice('')
      try {
        const attestation={started_outside_odin:true as const,file_revision_confirmed:true as const}
        const saved=qualify?await production.qualifyOutside(Number(printId),{
          printer_id:Number(printerId),...(fileId?{print_file_id:Number(fileId)}:{}),plate_index:Number(plate)-1,
          name,materials,qualification_confirmed:true,outputs,attestation,
        }):await production.bindOutside(Number(printId),{recipe_id:recipeId,attestation})
        resetConfirmations()
        setNotice(`Outside print ${saved.print_job_id} bound${qualify?' and recipe qualified':''}. Inspect the pieces before stock acceptance.`)
        await onBound()
      }catch(e){setError(e instanceof Error?e.message:'Could not bind this print. Reload and review its evidence.')}
      finally{setBusy(false)}
    }}>
      <p>Choose the completed observed print, or enter its physical print ID. Scheduled job and archive IDs are different. Eligibility is checked when binding.</p>
      <button type="button" className={button} disabled={busy} onClick={async()=>{setBusy(true);setError('');try{setRecent(await printJobs.list({status:'completed',limit:100}))}catch(e){setError(e instanceof Error?e.message:'Could not load observed prints.')}finally{setBusy(false)}}}>Load recent completed prints</button>
      {recent&&<label>Recent completed print<select className={input} disabled={busy} value={recent.some(p=>String(p.id)===printId)?printId:''} onChange={e=>{const selected=recent.find(p=>String(p.id)===e.target.value);if(selected){setPrintId(String(selected.id));setPrinterId(String(selected.printer_id));resetConfirmations()}}}><option value="">Choose observed print</option>{recent.map(p=><option value={p.id} key={p.id}>{String(p.job_name||p.filename||p.file_name||'Unnamed print')} · print {p.id} · {String(p.printer_name||`printer ${p.printer_id}`)}</option>)}</select></label>}
      {recent?.length===0&&<p>No recent completed prints found. Older physical print IDs can still be entered below.</p>}
      {error&&<p role="alert">{error}</p>}{notice&&<p role="status">{notice}</p>}
      <label>Outside print ID<input className={input} type="number" min="1" step="1" required disabled={busy} value={printId} onChange={e=>{setPrintId(e.target.value);resetConfirmations()}}/></label>
      {admin&&<label className="block"><input type="checkbox" disabled={busy} checked={qualify} onChange={e=>{setQualify(e.target.checked);resetConfirmations()}}/>Qualify a new recipe from this print</label>}
      {!qualify?<label>Existing qualified recipe<select className={input} required disabled={busy} value={recipeId} onChange={e=>{setRecipeId(e.target.value);resetConfirmations()}}><option value="">Choose recipe</option>{recipes.filter(r=>r.qualified).map(r=><option key={r.id} value={r.id}>{r.name}</option>)}</select></label>:<fieldset disabled={busy} className="space-y-3">
        <legend>New recipe from an owner-attested outside print</legend>
        <label>Observed printer ID<input className={input} type="number" min="1" step="1" required value={printerId} onChange={e=>{setPrinterId(e.target.value);resetConfirmations()}}/></label>
        <label>Print file ID (optional for a unique filename match)<input className={input} type="number" min="1" step="1" value={fileId} onChange={e=>{setFileId(e.target.value);resetConfirmations()}}/></label>
        <p>If the observed filename matches multiple files, enter the exact print file ID.</p>
        <label>Printed plate number<input className={input} type="number" min="1" step="1" required value={plate} onChange={e=>{setPlate(e.target.value);resetConfirmations()}}/></label>
        <label>Outside recipe name<input className={input} required maxLength={160} value={name} onChange={e=>{setName(e.target.value);setYieldConfirmed(false)}}/></label>
        <label>Outside recipe material and colors<textarea className={input} required maxLength={1000} value={materials} onChange={e=>{setMaterials(e.target.value);setYieldConfirmed(false)}}/></label>
        {outputs.map((o,i)=><fieldset key={i} className="space-y-2"><legend>Outside output {i+1}</legend>
          <label>Product<select aria-label={`Outside output ${i+1} product`} className={input} required value={o.product_id||''} onChange={e=>{setYieldConfirmed(false);setOutputs(old=>old.map((v,n)=>n===i?{...v,product_id:Number(e.target.value)}:v))}}><option value="">Choose product</option>{products.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
          <label>Output type<select aria-label={`Outside output ${i+1} type`} className={input} value={o.kind} onChange={e=>{setYieldConfirmed(false);setOutputs(old=>old.map((v,n)=>n===i?{...v,kind:e.target.value as 'main'|'flush'}:v))}}><option value="main">Main product</option><option value="flush">Sellable flush product</option></select></label>
          <label>Expected pieces<input aria-label={`Outside output ${i+1} expected pieces`} className={input} type="number" min="1" max="10000" step="1" required value={o.expected} onChange={e=>{setYieldConfirmed(false);setOutputs(old=>old.map((v,n)=>n===i?{...v,expected:Number(e.target.value)}:v))}}/></label>
          {outputs.length>1&&<button type="button" className={button} onClick={()=>{setYieldConfirmed(false);setOutputs(old=>old.filter((_,n)=>i!==n))}}>Remove outside output {i+1}</button>}
        </fieldset>)}
        <button type="button" className={button} disabled={outputs.length>=8} onClick={()=>{setYieldConfirmed(false);setOutputs(old=>[...old,{product_id:0,kind:'flush',expected:1}])}}>Add outside output</button>
        <label className="block"><input type="checkbox" checked={yieldConfirmed} onChange={e=>setYieldConfirmed(e.target.checked)}/>I verified the material setup and fixed yield of this outside print.</label>
      </fieldset>}
      <label className="block"><input type="checkbox" disabled={busy} checked={outside} onChange={e=>setOutside(e.target.checked)}/>I confirm this print was started outside ODIN.</label>
      <label className="block"><input type="checkbox" disabled={busy} checked={fileConfirmed} onChange={e=>setFileConfirmed(e.target.checked)}/>I confirm the exact file revision and printed plate.</label>
      <button className={button} type="submit" disabled={busy||!valid}>{qualify?'Qualify recipe and bind print':'Bind completed outside print'}</button>
    </form>}
  </div>
}
