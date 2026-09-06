import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, KeyRound, LogOut, Plus, RefreshCw, ShieldCheck, Trash2, TriangleAlert, X } from 'lucide-react'
import githubLogo from '@/assets/github.svg'
import { Button } from '@/components/ui/button'
import { CopyButton } from '@/components/copy-button'
import { Playground } from '@/components/playground'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { accessError, accountRequest, useSession, type AccountSession, type ApiKey } from '@/lib/account'
import { ApiError, dateLabel } from '@/lib/weather'

export function GithubLogo() { return <img className="github-logo" src={githubLogo} width="18" height="18" alt="" aria-hidden="true" /> }

export function ApiAccess({ loginError }: { loginError: string | null }) {
  const session = useSession()
  const [playgroundVersion, setPlaygroundVersion] = useState(0)
  return <section className="api-access">
    <div className="view-intro"><div><div className="eyebrow"><KeyRound size={13} />DEVELOPER ACCESS</div><h1>API access</h1><p>Weather forecasts for your applications.</p></div>
      <span className="tag">Experimental</span></div>
    {session.isPending ? <div className="account-loading" role="status">Loading account...</div> : session.isError ?
      <div className="account-message" role="alert"><TriangleAlert size={18} /><span>Account service unavailable</span><Button size="sm" variant="outline" onClick={() => void session.refetch()}><RefreshCw />Retry</Button></div> :
      session.data?.user ? <AccountWorkspace key={session.data.user.id} session={session.data} onKeyRevoked={() => setPlaygroundVersion(value => value + 1)} /> :
        <div id="api-account" className="sign-in-section">
          <div className="access-emblem"><GithubLogo /></div><h2>Sign in with GitHub</h2>
          {loginError && <p role="alert" className="account-error">{accessError(loginError)}</p>}
          {session.data?.configured ? <Button asChild className="github-signin"><a href="/api/auth/github"><GithubLogo />Continue with GitHub</a></Button>
            : <><Button className="github-signin" disabled><GithubLogo />Continue with GitHub</Button><p className="account-error" role="status">GitHub sign-in is not configured on this server.</p></>}
          <div className="access-permissions"><span><ShieldCheck size={15} />Read-only weather access</span><span>No repository permissions</span></div>
        </div>}
    <Playground key={`${session.isPending ? 'loading' : session.data?.user?.id ?? 'guest'}:${playgroundVersion}`} />
  </section>
}

function AccountWorkspace({ session, onKeyRevoked }: { session: AccountSession; onKeyRevoked: () => void }) {
  const queryClient = useQueryClient()
  const user = session.user!
  const keys = useQuery({ queryKey: ['account-keys', user.id], queryFn: ({ signal }) => accountRequest<{ keys: ApiKey[] }>('/v1/account/keys', { signal }), retry: false })
  const [label, setLabel] = useState('')
  const [busy, setBusy] = useState<'create' | 'revoke' | 'logout' | null>(null)
  const [secret, setSecret] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [revoke, setRevoke] = useState<ApiKey | null>(null)
  const active = keys.data?.keys.filter(key => !key.revoked_at).length ?? 0
  const headers = { 'Content-Type': 'application/json', 'x-csrf-token': session.csrf_token! }

  function failed(error: unknown) {
    setError(accessError(error))
    if (error instanceof ApiError && error.status === 401) {
      setSecret(null)
      queryClient.setQueryData<AccountSession>(['account-session'], { ...session, user: null, csrf_token: null })
      queryClient.removeQueries({ queryKey: ['account-keys'] })
    }
  }
  useEffect(() => {
    if (keys.error instanceof ApiError && keys.error.status === 401) void queryClient.invalidateQueries({ queryKey: ['account-session'] })
  }, [keys.error, queryClient])
  useEffect(() => {
    if (keys.isPending || window.location.hash !== '#playground') return
    const frame = window.requestAnimationFrame(() => document.getElementById('playground')?.scrollIntoView({ block: 'start' }))
    return () => window.cancelAnimationFrame(frame)
  }, [keys.isPending])
  async function create() {
    if (busy || !label.trim()) return
    setBusy('create'); setError(null); setNotice(null)
    try {
      // One-time secrets stay in this mounted component, never the query cache or storage.
      const created = await accountRequest<ApiKey & { key: string }>('/v1/account/keys', { method: 'POST', headers, body: JSON.stringify({ label: label.trim() }) })
      setSecret(created.key); setLabel('')
      await keys.refetch()
    } catch (error) { failed(error) } finally { setBusy(null) }
  }
  async function confirmRevoke() {
    if (!revoke || busy) return
    setBusy('revoke'); setError(null)
    try {
      await accountRequest<void>('/v1/account/keys/' + revoke.id, { method: 'DELETE', headers })
      onKeyRevoked()
      setSecret(null); setNotice('API key revoked.'); setRevoke(null)
      await keys.refetch()
    } catch (error) { failed(error) } finally { setBusy(null) }
  }
  async function logout() {
    setBusy('logout'); setError(null)
    try {
      await accountRequest<void>('/auth/logout', { method: 'POST', headers })
      setSecret(null)
      queryClient.setQueryData<AccountSession>(['account-session'], { ...session, user: null, csrf_token: null })
      queryClient.removeQueries({ queryKey: ['account-keys'] })
    } catch (error) { failed(error); setBusy(null) }
  }
  return <>
    <div className="account-identity"><GithubLogo /><div><strong>@{user.login}</strong><span>GitHub account</span></div>
      <Button size="sm" variant="outline" disabled={!!busy} onClick={() => void logout()}><LogOut />Sign out</Button></div>
    <dl className="access-limits"><div><dt>Access scope</dt><dd>Weather <span>read-only</span></dd></div>
      <div><dt>Requests / key / minute</dt><dd>{user.rate_limit_per_min}</dd></div><div><dt>Active keys</dt><dd>{active} <span>/ {session.max_active_keys}</span></dd></div></dl>
    {error && <p className="account-message account-error" role="alert"><TriangleAlert size={16} />{error}</p>}
    {notice && <p className="account-message" role="status"><Check size={16} />{notice}</p>}
    <section id="api-account" className="key-section"><div className="section-heading"><h2><KeyRound size={17} />API keys</h2><span className="muted">Shown once on creation</span></div>
      <form className="key-form" onSubmit={event => { event.preventDefault(); void create() }}><label htmlFor="key-label">Key name</label><div>
        <input id="key-label" value={label} onChange={event => setLabel(event.target.value)} placeholder="e.g. Weather dashboard" maxLength={64} required autoComplete="off" disabled={!!busy} />
        <Button type="submit" disabled={!!busy || !!secret || !keys.data || active >= session.max_active_keys || !label.trim()}><Plus />{busy === 'create' ? 'Creating...' : 'Create key'}</Button></div>
        {active >= session.max_active_keys && <p className="muted">Active-key limit reached.</p>}</form>
      {secret && <div className="new-key" role="status"><div><strong>Your new API key</strong><Button variant="ghost" size="icon" title="Dismiss secret" aria-label="Dismiss secret" onClick={() => setSecret(null)}><X /></Button></div>
        <div className="secret-value"><code>{secret}</code><CopyButton value={secret} label="Copy API key" /></div><p>This secret will not be shown again.</p></div>}
      {keys.isPending ? <p className="key-empty" role="status">Loading API keys...</p> : keys.isError ?
        <div className="account-message" role="alert"><span>{accessError(keys.error)}</span><Button size="sm" variant="outline" onClick={() => void keys.refetch()}><RefreshCw />Retry</Button></div> :
        !keys.data?.keys.length ? <p className="key-empty">No API keys yet.</p> :
          <Table><caption className="sr-only">Your API keys</caption><TableHeader><TableRow>{['Name', 'Key prefix', 'Created', 'Status', ''].map((name, i) => <TableHead key={i} scope="col">{name || <span className="sr-only">Actions</span>}</TableHead>)}</TableRow></TableHeader>
            <TableBody>{keys.data.keys.map(key => <TableRow key={key.id}><TableCell>{key.label || 'Untitled key'}</TableCell><TableCell><code>{key.prefix}...</code></TableCell>
              <TableCell>{dateLabel(key.created_at, 'Asia/Kolkata', { year: 'numeric' })}</TableCell><TableCell><span className={'key-status ' + (key.revoked_at ? 'revoked' : '')}>{key.revoked_at ? 'Revoked' : 'Active'}</span></TableCell>
              <TableCell className="key-actions">{!key.revoked_at && <Button size="icon" variant="ghost" disabled={!!busy} title={'Revoke ' + (key.label || 'key')} aria-label={'Revoke ' + (key.label || 'key')} onClick={() => { setError(null); setRevoke(key) }}><Trash2 /></Button>}</TableCell></TableRow>)}</TableBody></Table>}
    </section>
    {revoke && <RevokeDialog apiKey={revoke} error={error} busy={busy === 'revoke'} onClose={() => setRevoke(null)} onConfirm={() => void confirmRevoke()} />}
  </>
}

function RevokeDialog({ apiKey, error, busy, onClose, onConfirm }: { apiKey: ApiKey; error: string | null; busy: boolean; onClose: () => void; onConfirm: () => void }) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => { const dialog = ref.current!; dialog.showModal(); return () => dialog.close() }, [])
  return <dialog ref={ref} className="revoke-dialog" aria-labelledby="revoke-title" aria-describedby="revoke-description" onCancel={event => { event.preventDefault(); if (!busy) onClose() }}>
    <h2 id="revoke-title">Revoke API key?</h2><p id="revoke-description"><strong>{apiKey.label || apiKey.prefix}</strong> will stop working immediately. This cannot be undone.</p>
    {error && <p className="account-error" role="alert">{error}</p>}
    <div><Button variant="outline" onClick={onClose} disabled={busy} autoFocus>Cancel</Button><Button variant="destructive" onClick={onConfirm} disabled={busy}><Trash2 />{busy ? 'Revoking...' : 'Revoke key'}</Button></div>
  </dialog>
}
