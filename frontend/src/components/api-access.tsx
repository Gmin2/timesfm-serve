import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import githubLogo from '@/assets/github.svg'
import { Button } from '@/components/ui/button'
import { CopyButton } from '@/components/copy-button'
import { Playground } from '@/components/playground'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import * as Icon from './icons'
import { accessError, accountRequest, requestExample, useSession, type AccountSession, type ApiKey } from '@/lib/account'
import { ApiError, dateLabel } from '@/lib/weather'

const DOCS_URL = 'https://88novucbtj.execute-api.us-east-1.amazonaws.com/docs'

export function GithubLogo() { return <img className="github-logo" src={githubLogo} width="18" height="18" alt="" aria-hidden="true" /> }

export function ApiAccess({ loginError }: { loginError: string | null }) {
  const session = useSession()
  const [playgroundVersion, setPlaygroundVersion] = useState(0)
  return <section className="api-access">
    {session.isPending ? <div className="account-loading" role="status">Loading account...</div> : session.isError ?
      <div className="account-message" role="alert"><Icon.Warning />Account service unavailable<Button size="sm" variant="outline" onClick={() => void session.refetch()}><Icon.Refresh />Retry</Button></div> :
      session.data?.user ? <AccountWorkspace key={session.data.user.id} session={session.data} onKeyRevoked={() => setPlaygroundVersion(value => value + 1)} /> :
        <>
          <AccessHead />
          <div id="api-account" className="card sign-in-card">
            <span className="access-emblem"><GithubLogo /></span><h2>Sign in with GitHub</h2>
            <p>Create read-only keys for station forecasts. Keys are shown once and stored only as hashes.</p>
            {loginError && <p role="alert" className="account-error">{accessError(loginError)}</p>}
            {session.data?.configured ? <Button asChild variant="dark" className="github-signin"><a href="/api/auth/github"><GithubLogo />Continue with GitHub</a></Button>
              : <><Button variant="dark" className="github-signin" disabled><GithubLogo />Continue with GitHub</Button><p className="account-error" role="status">GitHub sign-in is not configured on this server.</p></>}
            <div className="access-permissions"><span><Icon.Shield size={14} />Read-only weather access</span><span>No repository permissions</span></div>
          </div>
        </>}
    <Playground key={`${session.isPending ? 'loading' : session.data?.user?.id ?? 'guest'}:${playgroundVersion}`} />
  </section>
}

function AccessHead({ children }: { children?: React.ReactNode }) {
  return <div className="page-head">
    <span className="page-tile"><Icon.Key size={18} /></span>
    <div className="page-title"><h1>API keys</h1><span className="badge">weather:read</span></div>
    <p>Weather forecasts for your applications.</p>
    <div className="page-actions"><Button variant="outline" size="sm" asChild><a href={DOCS_URL} target="_blank" rel="noreferrer"><Icon.Book />Docs</a></Button>{children}</div>
  </div>
}

function AccountWorkspace({ session, onKeyRevoked }: { session: AccountSession; onKeyRevoked: () => void }) {
  const queryClient = useQueryClient()
  const user = session.user!
  const keys = useQuery({ queryKey: ['account-keys', user.id], queryFn: ({ signal }) => accountRequest<{ keys: ApiKey[] }>('/v1/account/keys', { signal }), retry: false })
  const [busy, setBusy] = useState<'create' | 'revoke' | 'logout' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [revoke, setRevoke] = useState<ApiKey | null>(null)
  const [creating, setCreating] = useState(false)
  const active = keys.data?.keys.filter(key => !key.revoked_at).length ?? 0
  const atLimit = active >= session.max_active_keys
  const headers = { 'Content-Type': 'application/json', 'x-csrf-token': session.csrf_token! }

  function failed(error: unknown) {
    setError(accessError(error))
    if (error instanceof ApiError && error.status === 401) {
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
  async function create(label: string) {
    if (busy) return null
    setBusy('create'); setError(null); setNotice(null)
    try {
      // One-time secrets stay in the mounted sheet, never the query cache or storage.
      const created = await accountRequest<ApiKey & { key: string }>('/v1/account/keys', { method: 'POST', headers, body: JSON.stringify({ label }) })
      void keys.refetch()
      return created.key
    } catch (error) { failed(error); return null } finally { setBusy(null) }
  }
  async function confirmRevoke() {
    if (!revoke || busy) return
    setBusy('revoke'); setError(null)
    try {
      await accountRequest<void>('/v1/account/keys/' + revoke.id, { method: 'DELETE', headers })
      onKeyRevoked()
      setNotice('API key revoked.'); setRevoke(null)
      await keys.refetch()
    } catch (error) { failed(error) } finally { setBusy(null) }
  }
  async function logout() {
    setBusy('logout'); setError(null)
    try {
      await accountRequest<void>('/auth/logout', { method: 'POST', headers })
      queryClient.setQueryData<AccountSession>(['account-session'], { ...session, user: null, csrf_token: null })
      queryClient.removeQueries({ queryKey: ['account-keys'] })
    } catch (error) { failed(error); setBusy(null) }
  }
  return <>
    <AccessHead><Button size="sm" disabled={!!busy || !keys.data || atLimit} onClick={() => { setError(null); setNotice(null); setCreating(true) }}><Icon.Plus />Create key</Button></AccessHead>
    <div className="card account-strip">
      <div className="account-identity"><GithubLogo /><div><strong>@{user.login}</strong><span>GitHub account</span></div></div>
      <dl>
        <div><dt>Requests / key / minute</dt><dd>{user.rate_limit_per_min}</dd></div>
        <div><dt>Active keys</dt><dd>{active}<span> / {session.max_active_keys}</span></dd></div>
      </dl>
      <Button size="sm" variant="ghost" disabled={!!busy} onClick={() => void logout()}><Icon.SignOut />Sign out</Button>
    </div>
    {error && !creating && <p className="account-message account-error" role="alert"><Icon.Warning />{error}</p>}
    {notice && <p className="account-message" role="status"><Icon.CircleCheck />{notice}</p>}
    {atLimit && <p className="account-message muted">Active-key limit reached. Revoke a key to create another.</p>}
    <section id="api-account" className="card table-card key-section">
      {keys.isPending ? <p className="key-empty" role="status">Loading API keys...</p> : keys.isError ?
        <div className="account-message" role="alert"><span>{accessError(keys.error)}</span><Button size="sm" variant="outline" onClick={() => void keys.refetch()}><Icon.Refresh />Retry</Button></div> :
        !keys.data?.keys.length ? <div className="key-empty"><span className="row-tile"><Icon.Key size={13} /></span><p>No API keys yet.</p><small>Keys you create appear here.</small></div> :
          <Table><caption className="sr-only">Your API keys</caption><TableHeader><TableRow>{['Name', 'Status', 'Scope', 'Created', ''].map((name, i) => <TableHead key={i} scope="col">{name || <span className="sr-only">Actions</span>}</TableHead>)}</TableRow></TableHeader>
            <TableBody>{keys.data.keys.map(key => <TableRow key={key.id}>
              <TableCell><div className="cell-stack"><span className="row-tile"><Icon.Key size={13} /></span><div><strong>{key.label || 'Untitled key'}</strong><code>{key.prefix}...</code></div></div></TableCell>
              <TableCell><span className={'pill-status ' + (key.revoked_at ? 'revoked' : 'accepted')}>{key.revoked_at ? 'Revoked' : 'Active'}</span></TableCell>
              <TableCell><span className="chip">{key.scope}</span></TableCell>
              <TableCell>{dateLabel(key.created_at, 'Asia/Kolkata', { year: 'numeric' })}</TableCell>
              <TableCell className="key-actions">{!key.revoked_at && <Button size="icon" variant="ghost" disabled={!!busy} title={'Revoke ' + (key.label || 'key')} aria-label={'Revoke ' + (key.label || 'key')} onClick={() => { setError(null); setRevoke(key) }}><Icon.Trash /></Button>}</TableCell>
            </TableRow>)}</TableBody></Table>}
    </section>
    {creating && <CreateKeySheet session={session} busy={busy === 'create'} error={error} onCreate={create} onClose={() => { setCreating(false); setError(null) }} />}
    {revoke && <RevokeDialog apiKey={revoke} error={error} busy={busy === 'revoke'} onClose={() => setRevoke(null)} onConfirm={() => void confirmRevoke()} />}
  </>
}

const STEPS = ['Detail', 'Review', 'Secret key'] as const

function CreateKeySheet({ session, busy, error, onCreate, onClose }: {
  session: AccountSession; busy: boolean; error: string | null; onCreate: (label: string) => Promise<string | null>; onClose: () => void
}) {
  const ref = useRef<HTMLDialogElement>(null)
  const [step, setStep] = useState(0)
  const [label, setLabel] = useState('')
  const [secret, setSecret] = useState<string | null>(null)
  const user = session.user!
  useEffect(() => { const dialog = ref.current!; dialog.showModal(); return () => dialog.close() }, [])
  async function generate() {
    const key = await onCreate(label.trim())
    if (key) { setSecret(key); setStep(2) }
  }
  function close() { setSecret(null); onClose() }
  return <dialog ref={ref} className="sheet" aria-labelledby="sheet-title" onCancel={event => { event.preventDefault(); if (!busy) close() }}>
    <div className="sheet-rail">
      <h2 id="sheet-title">Create API key</h2>
      <ol>{STEPS.map((name, i) => <li key={name} aria-current={i === step ? 'step' : undefined} data-done={i < step || undefined}>
        <i>{i < step && <Icon.Check size={9} />}</i>{name}</li>)}</ol>
      <p className="sheet-rail-note"><Icon.Shield size={13} />Read-only weather access</p>
    </div>
    <form className="sheet-body" onSubmit={event => { event.preventDefault(); if (step === 0 && label.trim()) setStep(1); else if (step === 1) void generate() }}>
      <div className="sheet-content">
        {step === 0 ? <>
          <h3>Detail</h3><p className="sheet-sub">Name the key after the app or script that will use it.</p>
          <label htmlFor="key-label">Key name</label>
          <input id="key-label" className="input" value={label} onChange={event => setLabel(event.target.value)} placeholder="e.g. Weather dashboard" maxLength={64} required autoComplete="off" autoFocus />
          <small className="field-hint">{label.length} / 64</small>
        </> : step === 1 ? <>
          <h3>Review</h3><p className="sheet-sub">Credential for weather reads</p>
          <dl className="spec">
            <div><dt>Name</dt><dd>{label.trim()}</dd></div>
            <div><dt>Owner</dt><dd>@{user.login}</dd></div>
            <div><dt>Permissions</dt><dd><span className="chip">weather:read</span></dd></div>
            <div><dt>Rate limit</dt><dd>{user.rate_limit_per_min} requests / minute</dd></div>
            <div><dt>Replay credits</dt><dd>None</dd></div>
          </dl>
          <div className="code-card"><div className="code-card-head"><Icon.Terminal size={14} /><span>Using the key</span><code>GET /v1/weather/stations</code></div>
            <pre>{requestExample('curl', session.api_origin || '', '/v1/weather/stations')}</pre></div>
          {error && <p className="account-error" role="alert">{error}</p>}
        </> : <>
          <span className="success-mark"><Icon.Check size={14} /></span>
          <h3>Your new API key</h3><p className="sheet-sub">Copy it now. This secret will not be shown again.</p>
          {secret && <div className="id-box secret-value"><code>{secret}</code><CopyButton value={secret} label="Copy API key" /></div>}
          <p className="field-hint">Export it as <code>WEATHER_API_KEY</code> and send it in the <code>x-api-key</code> header.</p>
        </>}
      </div>
      <div className="sheet-footer">
        {step === 2 ? <Button type="button" className="sheet-primary" onClick={close}>Done</Button> : <>
          <Button type="button" variant="ghost" disabled={busy} onClick={() => step === 1 ? setStep(0) : close()}>{step === 1 ? 'Back' : 'Cancel'}</Button>
          <Button type="submit" className="sheet-primary" disabled={busy || !label.trim()}>{step === 0 ? 'Continue' : busy ? 'Generating...' : 'Generate key'}</Button>
        </>}
      </div>
    </form>
  </dialog>
}

function RevokeDialog({ apiKey, error, busy, onClose, onConfirm }: { apiKey: ApiKey; error: string | null; busy: boolean; onClose: () => void; onConfirm: () => void }) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => { const dialog = ref.current!; dialog.showModal(); return () => dialog.close() }, [])
  return <dialog ref={ref} className="revoke-dialog" aria-labelledby="revoke-title" aria-describedby="revoke-description" onCancel={event => { event.preventDefault(); if (!busy) onClose() }}>
    <h2 id="revoke-title">Revoke API key?</h2><p id="revoke-description"><strong>{apiKey.label || apiKey.prefix}</strong> will stop working immediately. This cannot be undone.</p>
    {error && <p className="account-error" role="alert">{error}</p>}
    <div><Button variant="outline" onClick={onClose} disabled={busy} autoFocus>Cancel</Button><Button variant="destructive" onClick={onConfirm} disabled={busy}><Icon.Trash />{busy ? 'Revoking...' : 'Revoke key'}</Button></div>
  </dialog>
}
