import { useEffect, useRef, useState } from 'react'

export default function RecoveryCodes({ codes, onSaved }: { codes: string[]; onSaved: () => void }) {
  const [notice, setNotice] = useState('')
  const dialog = useRef<HTMLDialogElement>(null)
  const heading = useRef<HTMLHeadingElement>(null)
  function openDialog() {
    const element = dialog.current
    if (!element || element.open) return
    if (element.showModal) element.showModal()
    else element.setAttribute('open', '')
    heading.current?.focus()
  }
  useEffect(() => {
    const element = dialog.current
    openDialog()
    return () => element?.close?.()
  }, [])

  async function copy() {
    try {
      await navigator.clipboard.writeText(codes.join('\n'))
      setNotice('Recovery codes copied.')
    } catch {
      setNotice('Copy was unavailable. Select the codes to copy them, or download the text file.')
    }
  }

  function download() {
    const url = URL.createObjectURL(new Blob([codes.join('\n') + '\n'], { type: 'text/plain' }))
    const link = document.createElement('a')
    link.href = url
    link.download = 'odin-recovery-codes.txt'
    link.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 0)
  }

  return <dialog ref={dialog} aria-labelledby="save-recovery-heading" onClose={openDialog} onCancel={event => event.preventDefault()}
    className="m-auto w-[calc(100%-2rem)] max-w-md max-h-[90dvh] overflow-auto rounded-md bg-[var(--brand-card-bg)] text-[var(--brand-text-primary)] p-5 space-y-3 backdrop:bg-black/60">
    <h2 id="save-recovery-heading" ref={heading} tabIndex={-1} className="text-lg font-semibold">Save your recovery codes</h2>
    <p>Keep these somewhere safe. Each code lets you sign in once if you lose your authenticator. You can only see this list now.</p>
    <ul className="grid grid-cols-1 min-[360px]:grid-cols-2 gap-1 font-mono">
      {codes.map((code, i) => <li key={i}>{code}</li>)}
    </ul>
    <div className="flex flex-wrap gap-4">
      <button type="button" onClick={copy} className="min-h-11 underline">Copy</button>
      <button type="button" onClick={download} className="min-h-11 underline">Download (.txt)</button>
    </div>
    {notice && <p role="status">{notice}</p>}
    <button type="button" onClick={onSaved} className="min-h-11 rounded px-4 py-2 bg-[var(--brand-primary)] text-[var(--brand-on-primary)]">I saved these</button>
  </dialog>
}
