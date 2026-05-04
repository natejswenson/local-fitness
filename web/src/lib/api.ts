import type {
  BriefResponse, BriefStreamEvent, ChatEvent, MetricSeries, SyncState,
  SyncTriggerResponse, TodayResponse, TrainingLoadSeries, Workout,
} from './types'

async function getJson<T>(url: string): Promise<T> {
  const r = await fetch(url)
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`)
  return r.json()
}

export const api = {
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
  brief: () => getJson<BriefResponse>('/api/brief'),
  briefGenerate: async (model: string) => {
    const r = await fetch('/api/brief/generate', {
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
    const r = await fetch('/api/brief/generate/stream', {
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
    const r = await fetch(url, { method: 'POST' })
    if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`)
    return r.json() as Promise<SyncTriggerResponse>
  },
  syncStatus: () => getJson<SyncState>('/api/sync/status'),
  chatEnd: (sessionId: string) =>
    fetch(`/api/chat/${sessionId}/end`, { method: 'POST' }),
  chat: async function* (
    sessionId: string,
    message: string,
    model: string,
    signal?: AbortSignal,
  ): AsyncGenerator<ChatEvent> {
    const r = await fetch('/api/chat', {
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
