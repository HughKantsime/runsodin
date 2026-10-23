import {useRef,useState} from 'react'
import {previewMaterialEmail,MaterialEmailPreview as Preview} from '../../api/materialPurchases'
export default function MaterialEmailPreview(){
 const [source,setSource]=useState(''),[result,setResult]=useState<Preview|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState('')
 const generation=useRef(0)
 const clear=()=>{generation.current++;setSource('');setResult(null);setError('')}
 const preview=async()=>{const version=++generation.current;setBusy(true);setError('');setResult(null);try{const value=await previewMaterialEmail(source);if(version===generation.current){setResult(value);setSource('')}}catch{if(version===generation.current)setError('Could not preview this email. Use a supported Amazon order confirmation or the structured purchase form.')}finally{setBusy(false)}}
 return <details className="space-y-3"><summary>Preview an Amazon order email</summary><p>Paste the original message source (.eml contents), including From and Subject headers. This is an on-demand preview, not a connected mailbox. The message is processed without saving its body. Do not paste payment credentials. Unsupported formats need structured entry.</p>
 <label className="block">Order email source<textarea className="w-full min-w-0 rounded border border-[var(--brand-card-border)] bg-[var(--brand-content-bg)] p-2" rows={5} maxLength={300000} value={source} disabled={busy} onChange={e=>{generation.current++;setSource(e.target.value);setResult(null);setError('')}}/></label>
 <div className="flex flex-wrap gap-2"><button type="button" disabled={busy||!source} onClick={()=>void preview()} className="rounded border px-3 py-2 disabled:opacity-50">Preview email without importing</button><button type="button" onClick={clear} className="rounded border px-3 py-2">Clear email preview</button></div>
 {busy&&<p role="status">Checking message format…</p>}{error&&<p role="alert">{error}</p>}
 {result&&<div className="space-y-2"><p>{result.message}</p>{result.candidates.map(c=><div key={c.candidate_reference} className="break-words rounded border p-3"><p>{c.title}</p><p>{c.vendor} order {c.order_reference} · {c.ordered_quantity} ordered quantity</p><p>{c.message}</p></div>)}<p>No purchase lines or available stock changed. Verify each material against an exact vendor mapping, check existing order lines for duplicates, then use the incoming-order form. Personal and unrelated purchases can be ignored.</p></div>}
 </details>
}
