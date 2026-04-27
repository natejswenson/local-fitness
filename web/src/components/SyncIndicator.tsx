import { useEffect, useRef, useState } from 'react'
import { AlertTriangle, Check, Loader2, RotateCw, Settings2 } from 'lucide-react'
import { api } from '@/lib/api'
import type { SyncState } from '@/lib/types'
import { cn } from '@/lib/utils'

const POLL_INTERVAL_MS = 3_000

type Props = {
  // Called once after a sync run completes successfully (so the parent can refetch).
  onCompleted?: () => void
}

export function SyncIndicator({ onCompleted }: Props) {
  const [state, setState] = useState<SyncState | null>(null)
  const [retrying, setRetrying] = useState(false)
  const wasRunningRef = useRef(false)
  const lastCompletedRef = useRef<string | null>(null)
  const initialKickoffRef = useRef(false)

  useEffect(() => {
    let cancelled = false
    let timer: number | undefined

    async function poll() {
      try {
        const s = await api.syncStatus()
        if (cancelled) return
        setState(s)

        // Smart mount: only fire syncStart on first poll if data is
        // actually stale AND we're not throttled. Reloading the page five
        // times in a row shouldn't fire five sync attempts.
        if (!initialKickoffRef.current) {
          initialKickoffRef.current = true
          const stale = s.days_behind > 0 || s.last_status === 'orphaned' || s.last_status === 'interrupted'
          const eligible = !s.is_running && s.seconds_until_eligible === 0
          if (stale && eligible) {
            api.syncStart().catch(() => {})
          }
        }

        // Detect a transition from running → done, or a fresh completion.
        const justFinished = wasRunningRef.current && !s.is_running
        const newCompletion = s.last_completed_at && s.last_completed_at !== lastCompletedRef.current
        if ((justFinished || (newCompletion && lastCompletedRef.current != null)) && s.last_status === 'success') {
          onCompleted?.()
        }
        wasRunningRef.current = s.is_running
        lastCompletedRef.current = s.last_completed_at

        // Poll fast while running, slow otherwise.
        timer = window.setTimeout(poll, s.is_running ? 1500 : POLL_INTERVAL_MS)
      } catch {
        timer = window.setTimeout(poll, POLL_INTERVAL_MS)
      }
    }

    poll()

    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [onCompleted])

  async function handleRetry() {
    setRetrying(true)
    try {
      await api.syncStart({ force: true })
    } catch {
      // ignore — pill will re-render based on next poll
    } finally {
      setRetrying(false)
    }
  }

  if (!state) return null

  if (state.is_running) {
    return (
      <Pill tone="info">
        <Loader2 className="size-3 animate-spin" />
        Syncing latest data…
      </Pill>
    )
  }

  if (state.last_status === 'not_configured') {
    return (
      <Pill tone="warn" title="Run `fitness setup` in your terminal to wire up Garmin">
        <Settings2 className="size-3" />
        Sync needs setup
      </Pill>
    )
  }

  if (state.last_status === 'auth_failure') {
    return (
      <PillWithRetry
        tone="bad"
        title={state.last_error ?? undefined}
        retrying={retrying}
        onRetry={handleRetry}
      >
        <AlertTriangle className="size-3" />
        Garmin auth failed
      </PillWithRetry>
    )
  }

  if (state.last_status === 'orphaned' || state.last_status === 'interrupted') {
    return (
      <PillWithRetry
        tone="bad"
        title={state.last_error ?? 'Previous sync did not finish cleanly'}
        retrying={retrying}
        onRetry={handleRetry}
      >
        <AlertTriangle className="size-3" />
        Sync interrupted
      </PillWithRetry>
    )
  }

  if (state.last_status === 'failure' || state.last_status === 'partial') {
    return (
      <PillWithRetry
        tone={state.last_status === 'partial' ? 'warn' : 'bad'}
        title={state.last_error ?? undefined}
        retrying={retrying}
        onRetry={handleRetry}
      >
        <AlertTriangle className="size-3" />
        {state.last_status === 'partial' ? 'Sync partial' : 'Sync failed'}
      </PillWithRetry>
    )
  }

  if (state.last_completed_at || state.data_through_date) {
    const tone = state.days_behind >= 2 ? 'warn' : 'muted'
    return (
      <Pill tone={tone} title={syncTooltip(state)}>
        <Check className="size-3" />
        {freshnessText(state)}
      </Pill>
    )
  }

  return null
}

function freshnessText(state: SyncState): string {
  if (!state.data_through_date) return 'Synced'
  const days = state.days_behind
  if (days === 0) return 'Synced through today'
  if (days === 1) return 'Synced through yesterday'
  const d = new Date(state.data_through_date + 'T00:00:00')
  const label = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
  return `Through ${label} · ${days}d behind`
}

function syncTooltip(state: SyncState): string {
  const parts: string[] = []
  if (state.data_through_date) parts.push(`Data through: ${state.data_through_date}`)
  if (state.last_completed_at) parts.push(`Last sync: ${new Date(state.last_completed_at).toLocaleString()}`)
  return parts.join(' · ')
}

function Pill({
  children, tone, title,
}: {
  children: React.ReactNode
  tone: 'info' | 'warn' | 'bad' | 'muted'
  title?: string
}) {
  return (
    <span
      title={title}
      className={cn(
        'inline-flex items-center gap-1.5 text-[11px] px-2.5 py-1 rounded-full border',
        tone === 'info' && 'border-accent-dim text-accent bg-accent/10',
        tone === 'warn' && 'border-warn/40 text-warn bg-warn/10',
        tone === 'bad' && 'border-bad/40 text-bad bg-bad/10',
        tone === 'muted' && 'border-border text-muted bg-surface',
      )}
    >
      {children}
    </span>
  )
}

function PillWithRetry({
  children, tone, title, retrying, onRetry,
}: {
  children: React.ReactNode
  tone: 'warn' | 'bad'
  title?: string
  retrying: boolean
  onRetry: () => void
}) {
  return (
    <span className="inline-flex items-center gap-1">
      <Pill tone={tone} title={title}>{children}</Pill>
      <button
        onClick={onRetry}
        disabled={retrying}
        className="text-[11px] inline-flex items-center gap-1 px-2 py-1 rounded-full border border-border bg-surface hover:bg-surface-2 transition-colors text-muted hover:text-text disabled:opacity-50"
        title="Retry sync"
      >
        {retrying ? <Loader2 className="size-3 animate-spin" /> : <RotateCw className="size-3" />}
        Retry
      </button>
    </span>
  )
}
