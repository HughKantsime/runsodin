import { useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { printers, type RestartQuestion as Question } from '../../api'
import { Button } from '../ui'

export default function RestartQuestion({ printerId, question, canAnswer, onNotice }: {
  printerId: number; question: Question; canAnswer: boolean; onNotice: (message: string) => void
}) {
  const client = useQueryClient()
  const submitting = useRef(false)
  const [notice, setNotice] = useState('')
  const mutation = useMutation({
    retry: false,
    mutationFn: (answer: 'same' | 'new') => printers.answerRestartQuestion(printerId, question.id, answer),
    onSuccess: () => onNotice('Your answer was recorded.'),
    onError: () => setNotice('Your answer could not be confirmed. Refresh the question before trying again.'),
    onSettled: async () => {
      await Promise.all([['dispatch-recovery', printerId], ['alerts'], ['print-jobs'], ['printers'], ['printer', printerId]].map(queryKey => client.invalidateQueries({ queryKey })))
      submitting.current = false
    },
  })
  function answer(value: 'same' | 'new') {
    if (submitting.current) return
    submitting.current = true
    mutation.mutate(value)
  }
  if (question.state === 'investigation') return <section aria-label="Print records need investigation" className="mb-4 min-w-0 space-y-3 rounded border border-[var(--brand-card-border)] p-3 text-sm break-words">
    <h3 className="font-medium">Print records need investigation</h3>
    <p>{question.message}</p>
    <p>Records: {question.candidate_ids?.join(', ')}. Ask an administrator to review these records before confirming the print.</p>
  </section>
  return <section aria-label="Confirm print after restart" className="mb-4 min-w-0 space-y-3 rounded border border-[var(--brand-card-border)] p-3 text-sm break-words">
    <h3 className="font-medium">Is it still the same print?</h3>
    <p>ODIN was recording {question.print_name} when it restarted. Confirm whether the printer is still making that print.</p>
    <p>Live readings continue while ODIN waits for your answer. Choosing a new print closes the earlier record with its outcome unknown.</p>
    <p>A linked scheduled job stays marked as printing if you choose a new print or this print ends unanswered. An administrator must review that job before releasing the retained dispatch.</p>
    <p>Your answer is saved even if ODIN restarts before recording resumes. After recording resumes, another restart may need a new confirmation.</p>
    {canAnswer ? <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap">
      <Button className="min-h-[44px] whitespace-normal" disabled={mutation.isPending} onClick={() => answer('same')}>Yes, same print: keep recording on it</Button>
      <Button className="min-h-[44px] whitespace-normal" variant="secondary" disabled={mutation.isPending} onClick={() => answer('new')}>No, it is a new print</Button>
    </div> : <p>An authorized operator or administrator can answer this question in Dispatch Recovery.</p>}
    {notice && <p role="status">{notice}</p>}
  </section>
}
