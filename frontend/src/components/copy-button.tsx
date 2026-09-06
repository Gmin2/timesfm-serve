import { useEffect, useState } from 'react'
import { Check, Copy } from 'lucide-react'
import { Button } from '@/components/ui/button'

export function CopyButton({ value, label }: { value: string; label: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  useEffect(() => { if (state !== 'idle') { const timer = setTimeout(() => setState('idle'), 2000); return () => clearTimeout(timer) } }, [state])
  return <span className="copy-control"><Button size="icon" variant="ghost" title={label} aria-label={label} onClick={async () => {
    try { await navigator.clipboard.writeText(value); setState('copied') } catch { setState('failed') }
  }}>{state === 'copied' ? <Check /> : <Copy />}</Button><span className="sr-only" role="status">{state === 'copied' ? 'Copied' : state === 'failed' ? 'Clipboard unavailable. Select the text to copy.' : ''}</span></span>
}
