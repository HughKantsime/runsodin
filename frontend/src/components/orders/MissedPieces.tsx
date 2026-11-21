import { useEffect, useState, type FormEvent } from 'react'
import { production, type Inspection, type Recipe, type RecipeOutput } from '../../api/production'
import { square } from '../../api/square'
import { InspectionFields, inspectionDetails } from './InspectionDetails'

const input = 'block w-full rounded border border-[var(--brand-card-border)] bg-[var(--brand-content-bg)] p-2'
const button = 'rounded border border-[var(--brand-card-border)] px-3 py-2 disabled:opacity-50'
type Props = {
  receipt: Inspection; products: {id:number; name:string}[]; admin: boolean
  close: () => void; saved: () => void
}

export default function MissedPieces({receipt, products, admin, close, saved}: Props) {
  const [recipes, setRecipes] = useState<Recipe[]>([])
  const [selected, setSelected] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [extra, setExtra] = useState<RecipeOutput[]>([{product_id:0, kind:'flush', expected:1}])
  const [busy, setBusy] = useState(true), [error, setError] = useState('')
  useEffect(() => {
    let active = true
    async function load() {
      const all: Recipe[] = []
      let offset: number | null = 0
      do {
        const page = await production.state(offset)
        all.push(...page.recipes)
        offset = page.next_offset
      } while (offset !== null)
      if (active) {
        setRecipes(all)
        const matches = all.filter(r => r.qualified && r.extends_recipe_id === receipt.recipe_id)
        setSelected(matches.length === 1 ? matches[0].id : '')
      }
    }
    load().catch(e => {if (active) setError(e.message)}).finally(() => {if (active) setBusy(false)})
    return () => {active = false}
  }, [receipt.recipe_id])

  const base = recipes.find(r => r.id === receipt.recipe_id)
  const revisions = recipes.filter(r => r.qualified && r.extends_recipe_id === receipt.recipe_id)
  const revision = revisions.find(r => r.id === selected)
  const outputs = revision?.outputs.filter(o => !receipt.outputs.some(first => first.product_id === o.product_id && first.kind === o.kind)) ?? []
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const fields = new FormData(event.currentTarget), details = inspectionDetails(event.currentTarget)
    if (!confirmed) return
    setBusy(true)
    setError('')
    try {
      if (!revision && base?.reference_job_id && admin) {
        const original = base.outputs.map(({product_id,kind,expected}) => ({product_id,kind,expected}))
        const result = await production.recipe({extends_recipe_id:base.id, reference_job_id:base.reference_job_id,
          name:base.name, materials:base.materials, qualification_confirmed:true, outputs:[...original,...extra]})
        setRecipes(old => [...old,result])
        setConfirmed(false)
        setSelected(result.id)
      } else if (revision) {
        const counts = outputs.map((o,i) => {
          const choice = fields.get('baseline'+i)
          const baseline_disposition: 'send'|'in_baseline'|undefined = choice === 'send' || choice === 'in_baseline' ? choice : undefined
          return {product_id:o.product_id, kind:o.kind, accepted:Number(fields.get('accepted'+i)),
            rejected:Number(fields.get('rejected'+i)), baseline_disposition}
        })
        if (counts.some((o,i) => o.accepted + o.rejected !== outputs[i].expected)) {
          throw new Error('The accepted and rejected pieces must add up to the expected count for each output.')
        }
        const squareRevision = counts.some(o => o.accepted > 0) ? (await square.status()).revision : null
        await production.inspect({...details, recipe_id:revision.id, job_id:receipt.job_id, supplement_of:receipt.id,
          inspection_confirmed:true, not_already_added_confirmed:true, square_revision:squareRevision, outputs:counts})
        saved()
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save these pieces. Check the inspection and try again.')
    } finally { setBusy(false) }
  }
  return <div className="space-y-2">
    <h4>Add pieces missed at inspection</h4>
    <p>The original count above stays on its receipt.</p>
    {error && <p role="alert">{error}</p>}
    {busy && <p role="status">Working…</p>}
    <label className="block">Extended recipe<select className={input} disabled={busy} value={selected} onChange={e=>{setSelected(e.target.value);setConfirmed(false)}}>
      <option value="">{admin?'Create a revision':'Choose a revision'}</option>
      {revisions.map(r=><option key={r.id} value={r.id}>{r.name} · {r.id.slice(0,8)}</option>)}
    </select></label>
    <form key={selected} onSubmit={submit} onChange={event=>{
      if (!(event.target instanceof HTMLInputElement && event.target.type === 'checkbox')) setConfirmed(false)
    }}><fieldset className="space-y-2" disabled={busy}>
      {revision ? <>
        {outputs.map((o,i)=><fieldset key={o.product_id} className="space-y-2"><legend>{o.product_name} · {o.expected} flush pieces</legend>
          <label>Accepted pieces<input className={input} type="number" name={'accepted'+i} min={0} max={o.expected} required/></label>
          <label>Rejected pieces<input className={input} type="number" name={'rejected'+i} min={0} max={o.expected} required/></label>
          <label>Baseline count<select className={input} name={'baseline'+i}><option value="">Choose if needed</option>
            <option value="in_baseline">Counted in baseline</option><option value="send">Not counted, send</option></select></label>
        </fieldset>)}
        <InspectionFields disabled={busy}/>
        <label className="block"><input type="checkbox" checked={confirmed} onChange={e=>setConfirmed(e.target.checked)} required/> I inspected these pieces and confirmed they have not already been added to Square by hand.</label>
        <button className={button} disabled={!confirmed}>Record additional pieces</button>
      </> : admin && base?.reference_job_id ? <>
        <p>Reference job {base.reference_job_id} · {base.materials}</p>
        {extra.map((o,i)=><fieldset key={i}><legend>Added flush output {i+1}</legend>
          <label>Product<select className={input} required value={o.product_id||''} onChange={e=>setExtra(old=>old.map((v,n)=>n===i?{...v,product_id:Number(e.target.value)}:v))}>
            <option value="">Choose product</option>{products.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
          <label>Expected pieces<input className={input} required type="number" min={1} max={10000} value={o.expected||''} onChange={e=>setExtra(old=>old.map((v,n)=>n===i?{...v,expected:Number(e.target.value)}:v))}/></label>
          {extra.length>1 && <button className={button} type="button" onClick={()=>{setExtra(old=>old.filter((_,n)=>n!==i));setConfirmed(false)}}>Remove output</button>}
        </fieldset>)}
        {base.outputs.length+extra.length<8 && <button className={button} type="button" onClick={()=>{setExtra(old=>[...old,{product_id:0,kind:'flush',expected:1}]);setConfirmed(false)}}>Add another flush output</button>}
        <label className="block"><input type="checkbox" checked={confirmed} onChange={e=>setConfirmed(e.target.checked)} required/> I verified the added pieces and their yield on the reference job.</label>
        <button className={button} disabled={!confirmed}>Save extended recipe</button>
      </> : !busy && <p>Choose a qualified revision that includes the missed pieces, or ask an administrator to add one.</p>}
    </fieldset></form>
    <button type="button" disabled={busy} className={button} onClick={close}>Close</button>
  </div>
}
