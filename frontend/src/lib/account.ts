import { useQuery } from '@tanstack/react-query'
import { ApiError } from './weather'

export interface AccountSession {
  configured: boolean
  user: { id: number; github_id: string; login: string; rate_limit_per_min: number; expires_at: string } | null
  csrf_token: string | null
  api_origin: string | null
  max_active_keys: number
}
export interface ApiKey {
  id: number; prefix: string; label: string | null; created_at: string; revoked_at: string | null; scope: string
}
export async function accountRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch('/api' + path, { credentials: 'same-origin', ...init,
    headers: { Accept: 'application/json', ...init.headers } })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new ApiError(response.status, typeof body.detail === 'string' ? body.detail : 'request_failed')
  }
  return response.status === 204 ? undefined as T : response.json()
}
export function useSession() {
  return useQuery({ queryKey: ['account-session'], queryFn: ({ signal }) => accountRequest<AccountSession>('/auth/session', { signal }),
    staleTime: 30_000, refetchInterval: 60_000, retry: false })
}
export function accessError(error: unknown): string {
  const messages: Record<string, string> = {
    active_key_limit: 'The active-key limit has been reached.', daily_key_limit: 'Daily key-creation limit reached. Try again tomorrow.',
    csrf_validation_failed: 'Your session changed. Refresh the page and try again.', session_expired: 'Your session expired. Sign in again.',
    key_not_found: 'This key is no longer active.', rate_limit_exceeded: 'Too many requests. Wait a minute and try again.',
    github_access_denied: 'GitHub sign-in was cancelled.', github_login_failed: 'GitHub sign-in failed. Please try again.',
    invalid_login_state: 'The sign-in request expired or changed. Please start again.', account_suspended: 'This account is suspended.',
  }
  return messages[error instanceof ApiError ? error.detail : String(error)] || 'The account service is unavailable. Please try again.'
}
export function requestExample(language: 'curl' | 'python', origin: string, path: string): string {
  const url = origin + path
  return language === 'curl'
    ? `curl --fail-with-body '${url}' \\\n  --header "x-api-key: $WEATHER_API_KEY"`
    : `import os\nimport requests\n\nresponse = requests.get(\n    ${JSON.stringify(url)},\n    headers={"x-api-key": os.environ["WEATHER_API_KEY"]},\n    timeout=15,\n)\nresponse.raise_for_status()\nprint(response.json())`
}
