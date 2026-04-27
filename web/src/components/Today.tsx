import { useEffect, useRef, useState } from 'react'
import {
  Activity, Bike, ChevronDown, ChevronRight, Dumbbell, Footprints,
  HandMetal, Loader2, Mountain, RefreshCw, Sparkles, Waves,
} from 'lucide-react'
import { api } from '@/lib/api'
import type { Brief, Takeaway, Workout } from '@/lib/types'
import { Card, CardBody } from './Card'
import { ChatPanel } from './ChatPanel'
import { SyncIndicator } from './SyncIndicator'
import { TakeawayCard } from './TakeawayCard'
import { cn, fmtDate, fmtKm, fmtPace, fmtSeconds } from '@/lib/utils'

type SeedRequest = { text: string; nonce: number }

export function Today() {
  const [brief, setBrief] = useState<Brief | null>(null)
  const [dataThrough, setDataThrough] = useState<string | null>(null)
  const [workouts, setWorkouts] = useState<Workout[] | null>(null)
  const [briefLoading, setBriefLoading] = useState(false)
  const [showWorkouts, setShowWorkouts] = useState(false)
  const [userName, setUserName] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [seedRequest, setSeedRequest] = useState<SeedRequest | null>(null)
  const autoRegenAttemptedRef = useRef(false)

  useEffect(() => {
    Promise.all([api.brief(), api.workouts({ days: 30, limit: 8 }), api.config()])
      .then(([b, w, c]) => {
        setBrief(b.brief)
        setDataThrough(b.data_through_date)
        setWorkouts(w.workouts)
        setUserName(c.user_name)
        // Auto-regen on first visit if no brief exists yet today AND we have data.
        // One-shot per page load — guard with a ref so React strict-mode double
        // mount doesn't fire twice.
        if (!b.brief && b.data_through_date && !autoRegenAttemptedRef.current) {
          autoRegenAttemptedRef.current = true
          regenerateBrief()
        }
      })
      .catch((e) => setError(String(e)))
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Triggered by SyncIndicator when a background pull just completed
  // successfully — quietly refetch workouts so any newly-arrived data shows.
  function onSyncCompleted() {
    api.workouts({ days: 30, limit: 8 }).then((w) => setWorkouts(w.workouts)).catch(() => {})
    api.brief().then((b) => setDataThrough(b.data_through_date)).catch(() => {})
  }

  async function regenerateBrief() {
    setBriefLoading(true)
    try {
      const r = await api.briefGenerate('claude-sonnet-4-6')
      setBrief(r.brief)
      setDataThrough(r.data_through_date)
    } catch (e) {
      setError(String(e))
    } finally {
      setBriefLoading(false)
    }
  }

  function askAbout(t: Takeaway) {
    const text = `Tell me more about: "${t.headline}"`
    setSeedRequest((prev) => ({ text, nonce: (prev?.nonce ?? 0) + 1 }))
  }

  if (error) return <div className="p-6 text-bad">{error}</div>

  const today = new Date()
  const greeting = today.toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric' })
  const briefIsStale = isBriefStale(brief, dataThrough)

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-6xl mx-auto px-4 sm:px-6 py-5 sm:py-8 space-y-4 sm:space-y-5">
        {/* Header — personalised greeting + sync status + regenerate.
            Stacks vertically on mobile (greeting line; then pill + button
            on their own row) so nothing wraps awkwardly on a phone. */}
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between sm:gap-4">
          <div>
            <div className="text-sm text-muted">{greeting}</div>
            <h1 className="text-xl sm:text-2xl font-semibold tracking-tight mt-0.5">
              {userName ? `${timeOfDayGreeting()}, ${userName}` : 'Today'}
            </h1>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            <SyncIndicator onCompleted={onSyncCompleted} />
            <button
              onClick={regenerateBrief}
              disabled={briefLoading}
              className="text-xs text-muted hover:text-text inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full border border-border bg-surface hover:bg-surface-2 transition-colors disabled:opacity-60"
              title="Regenerate brief"
            >
              {briefLoading ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
              {brief ? 'Regenerate' : 'Generate brief'}
            </button>
          </div>
        </div>

        {/* Stale brief banner — gracefully wraps on narrow viewports */}
        {brief && briefIsStale && !briefLoading && (
          <button
            onClick={regenerateBrief}
            className="w-full flex flex-col sm:flex-row items-start sm:items-center justify-between gap-2 sm:gap-3 px-4 py-3 rounded-xl border border-warn/40 bg-warn/10 text-warn hover:bg-warn/15 transition-colors text-left text-[13px] sm:text-sm"
          >
            <span className="inline-flex items-start gap-2">
              <Sparkles className="size-4 mt-0.5 shrink-0" />
              <span>Newer data available — your brief was generated before today's data landed</span>
            </span>
            <span className="text-xs font-medium underline-offset-2 hover:underline shrink-0">Regenerate</span>
          </button>
        )}

        {/* Key Takeaways — multi-column on lg+ to use horizontal space and
            keep the brief above-the-fold. Each card is compact by default
            (sparkline thumbnail, headline, summary, action row) and expands
            inline to show the full chart + details. */}
        {brief ? (
          <div className="space-y-3">
            <div className="text-xs font-medium uppercase tracking-wider text-muted">
              Key Takeaways
            </div>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
              {brief.takeaways.map((t, i) => (
                <TakeawayCard key={i} takeaway={t} onAsk={() => askAbout(t)} />
              ))}
            </div>
          </div>
        ) : (
          <Card>
            <div className="p-8 text-center">
              {briefLoading ? (
                <div className="flex flex-col items-center gap-3 text-muted">
                  <Loader2 className="size-5 animate-spin text-accent" />
                  <div className="text-sm">Reading your data…</div>
                  <div className="text-xs text-faint">First takeaways usually take 30–60 seconds</div>
                </div>
              ) : (
                <div className="text-sm text-muted">
                  No brief yet for today. Click "Generate brief" above.
                </div>
              )}
            </div>
          </Card>
        )}

        {/* Subtle divider before the conversation */}
        <div className="border-t border-border my-2" />

        {/* Embedded chat */}
        <ChatPanel seedRequest={seedRequest} />

        {/* Recent workouts — collapsed by default */}
        <div>
          <button
            onClick={() => setShowWorkouts((v) => !v)}
            className="w-full flex items-center justify-between text-xs font-medium uppercase tracking-wider text-muted hover:text-text transition-colors py-2"
          >
            <span className="inline-flex items-center gap-2">
              {showWorkouts ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
              Recent Workouts
              {workouts && <span className="text-faint normal-case tracking-normal">({workouts.length})</span>}
            </span>
          </button>
          {showWorkouts && workouts && workouts.length > 0 && (
            <Card className="mt-2">
              {/* Mobile: stacked cards; readable at narrow widths without
                  horizontal scroll. */}
              <div className="sm:hidden divide-y divide-border">
                {workouts.map((w) => {
                  const Icon = activityIcon(w.activity_type)
                  const loadStyle = trainingLoadStyle(w.training_load)
                  return (
                    <div key={w.activity_id} className="px-4 py-3 flex items-start gap-3">
                      <span className="size-9 rounded-lg bg-surface-2 flex items-center justify-center shrink-0">
                        <Icon className="size-4 text-muted" />
                      </span>
                      <div className="flex-1 min-w-0 space-y-1">
                        <div className="flex items-center justify-between gap-2">
                          <span className="text-[13px] font-medium text-text">
                            {fmtDate(w.date)}
                          </span>
                          {w.training_load != null && (
                            <span
                              className={cn(
                                'inline-flex items-center justify-center min-w-[2.25rem] px-1.5 py-0.5 rounded-md text-[11px] font-medium tabular-nums',
                                loadStyle,
                              )}
                              title={loadTooltip(w.training_load)}
                            >
                              Load {w.training_load.toFixed(0)}
                            </span>
                          )}
                        </div>
                        <div className="text-[13px] text-muted capitalize">
                          {w.activity_type.replace(/_/g, ' ')}
                          {w.distance_meters != null && (
                            <> · <span className="text-text tabular-nums">{fmtKm(w.distance_meters)}</span></>
                          )}
                          {w.duration_seconds != null && (
                            <> · <span className="text-text tabular-nums">{fmtSeconds(w.duration_seconds)}</span></>
                          )}
                        </div>
                        <div className="text-[12px] text-muted tabular-nums flex flex-wrap gap-x-3 gap-y-0.5">
                          {w.avg_pace_sec_per_km != null && (
                            <span>Pace {fmtPace(w.avg_pace_sec_per_km)}</span>
                          )}
                          {w.avg_hr != null && (
                            <span>HR {w.avg_hr}</span>
                          )}
                        </div>
                      </div>
                    </div>
                  )
                })}
              </div>

              {/* Desktop: full table. */}
              <CardBody className="hidden sm:block">
                <div className="overflow-x-auto -mx-5 px-5">
                  <table className="w-full text-sm">
                    <thead className="text-xs text-muted">
                      <tr className="text-left">
                        <th className="font-medium pb-2 pr-4">Date</th>
                        <th className="font-medium pb-2 pr-4">Activity</th>
                        <th className="font-medium pb-2 pr-4 text-right">Distance</th>
                        <th className="font-medium pb-2 pr-4 text-right">Duration</th>
                        <th className="font-medium pb-2 pr-4 text-right">Pace</th>
                        <th className="font-medium pb-2 pr-4 text-right">HR</th>
                        <th className="font-medium pb-2 text-right">Load</th>
                      </tr>
                    </thead>
                    <tbody className="tabular-nums">
                      {workouts.map((w) => {
                        const Icon = activityIcon(w.activity_type)
                        const loadStyle = trainingLoadStyle(w.training_load)
                        return (
                          <tr key={w.activity_id} className="border-t border-border hover:bg-surface/50 transition-colors">
                            <td className="py-2 pr-4 whitespace-nowrap">
                              <span className="text-text">{fmtDate(w.date)}</span>
                            </td>
                            <td className="py-2 pr-4 text-muted">
                              <span className="inline-flex items-center gap-2">
                                <span className="size-6 rounded-md bg-surface-2 flex items-center justify-center shrink-0">
                                  <Icon className="size-3.5 text-muted" />
                                </span>
                                <span className="capitalize">{w.activity_type.replace(/_/g, ' ')}</span>
                              </span>
                            </td>
                            <td className="py-2 pr-4 text-right">{fmtKm(w.distance_meters)}</td>
                            <td className="py-2 pr-4 text-right">{fmtSeconds(w.duration_seconds)}</td>
                            <td className="py-2 pr-4 text-right text-muted">{fmtPace(w.avg_pace_sec_per_km)}</td>
                            <td className="py-2 pr-4 text-right">{w.avg_hr ?? '—'}</td>
                            <td className="py-2 text-right">
                              {w.training_load == null ? (
                                <span className="text-faint">—</span>
                              ) : (
                                <span
                                  className={cn(
                                    'inline-flex items-center justify-center min-w-[2.25rem] px-1.5 py-0.5 rounded-md text-[12px] font-medium tabular-nums',
                                    loadStyle,
                                  )}
                                  title={loadTooltip(w.training_load)}
                                >
                                  {w.training_load.toFixed(0)}
                                </span>
                              )}
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              </CardBody>
            </Card>
          )}
        </div>
      </div>
    </div>
  )
}

function isBriefStale(brief: Brief | null, dataThrough: string | null): boolean {
  if (!brief?.generated_at || !dataThrough) return false
  return brief.generated_at.slice(0, 10) < dataThrough
}

function timeOfDayGreeting(): string {
  const h = new Date().getHours()
  if (h < 12) return 'Good morning'
  if (h < 17) return 'Good afternoon'
  return 'Good evening'
}

// Lucide icon picker for the most common Garmin activity_type values.
// Falls back to a generic Activity icon for anything unknown.
function activityIcon(type: string) {
  const t = (type || '').toLowerCase()
  if (t.includes('run') || t.includes('treadmill')) return Footprints
  if (t.includes('walk')) return Footprints
  if (t.includes('hik') || t.includes('trail')) return Mountain
  if (t.includes('cycl') || t.includes('bik')) return Bike
  if (t.includes('swim')) return Waves
  if (t.includes('strength') || t.includes('weight')) return Dumbbell
  if (t.includes('yoga') || t.includes('stretch')) return HandMetal
  return Activity
}

// Color the training_load chip by intensity band so a heavy day pops
// against an easy day at a glance. Bands tuned for Nate's typical
// workout loads (most outdoor runs ~50-100, longer/harder sessions 100+).
function trainingLoadStyle(load: number | null): string {
  if (load == null) return ''
  if (load >= 150) return 'bg-bad/15 text-bad border border-bad/30'
  if (load >= 80) return 'bg-warn/15 text-warn border border-warn/30'
  if (load >= 30) return 'bg-accent/15 text-accent border border-accent-dim'
  return 'bg-surface-2 text-muted border border-border'
}

function loadTooltip(load: number): string {
  if (load >= 150) return `Training load ${load.toFixed(0)} — very hard session`
  if (load >= 80) return `Training load ${load.toFixed(0)} — hard session`
  if (load >= 30) return `Training load ${load.toFixed(0)} — moderate session`
  return `Training load ${load.toFixed(0)} — easy session`
}
