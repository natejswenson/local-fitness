import type {
  ActivityHeatmapResponse, BriefResponse, BriefStreamEvent, ChatEvent,
  MetricSeries, PaceEfficiencyResponse, StrengthVolumeResponse, SyncState,
  SyncTriggerResponse, TodayResponse, TrainingLoadSeries, Workout,
} from './types'

// --- Auth token ---------------------------------------------------------
// The server gates /api/* with a bearer token when LOCAL_FITNESS_API_TOKEN
// is set. The frontend stores the token in localStorage; the AuthGate
// component prompts for it on first load (and on any 401 response).

const TOKEN_KEY = 'local-fitness:api-token'

export const authToken = {
  get: (): string | null => {
    try {
      return window.localStorage.getItem(TOKEN_KEY)
    } catch {
      return null
    }
  },
  set: (value: string) => {
    try {
      window.localStorage.setItem(TOKEN_KEY, value)
    } catch {
      // localStorage disabled (private mode, etc.) — the AuthGate will
      // re-prompt on every load. Acceptable degradation.
    }
  },
  clear: () => {
    try {
      window.localStorage.removeItem(TOKEN_KEY)
    } catch { /* see above */ }
  },
}

/**
 * Thrown by the fetch wrapper when the server rejects the bearer token.
 * The AuthGate listens for this and re-shows the token-entry form so the
 * user can paste a fresh token without reloading the page.
 */
export class AuthRequiredError extends Error {
  constructor() {
    super('Auth required')
    this.name = 'AuthRequiredError'
  }
}

const _onAuthRequiredHandlers = new Set<() => void>()

export function onAuthRequired(handler: () => void): () => void {
  _onAuthRequiredHandlers.add(handler)
  return () => _onAuthRequiredHandlers.delete(handler)
}

function _signalAuthRequired() {
  authToken.clear()
  for (const h of _onAuthRequiredHandlers) {
    try { h() } catch { /* swallow per-handler */ }
  }
}

function withAuth(init?: RequestInit): RequestInit {
  const token = authToken.get()
  if (!token) return init ?? {}
  const headers = new Headers(init?.headers)
  headers.set('Authorization', `Bearer ${token}`)
  return { ...init, headers }
}

async function authedFetch(input: string, init?: RequestInit): Promise<Response> {
  const r = await fetch(input, withAuth(init))
  if (r.status === 401) {
    _signalAuthRequired()
    throw new AuthRequiredError()
  }
  return r
}

async function getJson<T>(url: string): Promise<T> {
  const r = await authedFetch(url)
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`)
  return r.json()
}

export const api = {
  /** Probe whether auth is required AND whether the current token works.
   * Returns `{ ok: true }` when accepted (or when auth isn't configured),
   * throws `AuthRequiredError` when the server says 401. */
  authVerify: () => getJson<{ ok: boolean; auth_required: boolean }>('/api/auth/verify'),
  status: () => getJson<unknown>('/api/status'),
  config: () => getJson<{ user_name: string; settings: Record<string, string> }>('/api/config'),
  today: () => getJson<TodayResponse>('/api/today'),
  metric: (name: string, days = 90) => getJson<MetricSeries>(`/api/metric/${name}?days=${days}`),
  trainingLoad: (days = 180) => getJson<TrainingLoadSeries>(`/api/training-load?days=${days}`),
  workouts: (opts: { activity_type?: string; days?: number; limit?: number } = {}) => {
    const p = new URLSearchParams()
    if (opts.activity_type) p.set('activity_type', opts.activity_type)
    if (opts.days) p.set('days', String(opts.days))
    if (opts.limit) p.set('limit', String(opts.limit))
    return getJson<{ workouts: Workout[] }>(`/api/workouts?${p}`)
  },
  activityHeatmap: (days = 365) =>
    getJson<ActivityHeatmapResponse>(`/api/activity-heatmap?days=${days}`),
  strengthVolume: (weeks = 104) =>
    getJson<StrengthVolumeResponse>(`/api/strength-volume?weeks=${weeks}`),
  paceEfficiency: (days = 180, minDistanceKm = 2) =>
    getJson<PaceEfficiencyResponse>(
      `/api/pace-efficiency?days=${days}&min_distance_km=${minDistanceKm}`,
    ),
  brief: () => getJson<BriefResponse>('/api/brief'),
  briefGenerate: async (model: string) => {
    const r = await authedFetch('/api/brief/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model }),
    })
    if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`)
    return r.json() as Promise<BriefResponse>
  },
  briefGenerateStream: async function* (
    model: string,
    signal?: AbortSignal,
  ): AsyncGenerator<BriefStreamEvent> {
    const r = await authedFetch('/api/brief/generate/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model }),
      signal,
    })
    if (!r.ok || !r.body) throw new Error(`${r.status}: ${await r.text()}`)
    const reader = r.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      let idx: number
      while ((idx = buf.indexOf('\n')) !== -1) {
        const line = buf.slice(0, idx).trim()
        buf = buf.slice(idx + 1)
        if (!line) continue
        try {
          yield JSON.parse(line) as BriefStreamEvent
        } catch (e) {
          console.warn('bad brief stream line', line, e)
        }
      }
    }
  },
  syncStart: async (opts: { force?: boolean } = {}) => {
    const url = opts.force ? '/api/sync?force=true' : '/api/sync'
    const r = await authedFetch(url, { method: 'POST' })
    if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`)
    return r.json() as Promise<SyncTriggerResponse>
  },
  syncStatus: () => getJson<SyncState>('/api/sync/status'),
  chatEnd: (sessionId: string) =>
    authedFetch(`/api/chat/${sessionId}/end`, { method: 'POST' }),
  chat: async function* (
    sessionId: string,
    message: string,
    model: string,
    signal?: AbortSignal,
  ): AsyncGenerator<ChatEvent> {
    const r = await authedFetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, message, model }),
      signal,
    })
    if (!r.ok || !r.body) throw new Error(`${r.status}: ${await r.text()}`)
    const reader = r.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      let idx: number
      while ((idx = buf.indexOf('\n')) !== -1) {
        const line = buf.slice(0, idx).trim()
        buf = buf.slice(idx + 1)
        if (!line) continue
        try {
          yield JSON.parse(line) as ChatEvent
        } catch (e) {
          console.warn('bad ndjson line', line, e)
        }
      }
    }
  },
}
