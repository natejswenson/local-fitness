import { useEffect, useRef, useState } from 'react'
import { ChevronDown, ChevronRight, Loader2, RefreshCw, Sparkles } from 'lucide-react'
import { api } from '@/lib/api'
import type { Brief, Takeaway, Workout } from '@/lib/types'
import { Card, CardBody } from './Card'
import { ChatPanel } from './ChatPanel'
import { SyncIndicator } from './SyncIndicator'
import { TakeawayCard } from './TakeawayCard'
import { fmtKm, fmtPace, fmtSeconds } from '@/lib/utils'

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
      <div className="max-w-3xl mx-auto px-6 py-8 space-y-5">
        {/* Header — personalised greeting + sync status + regenerate */}
        <div className="flex items-end justify-between gap-4">
          <div>
            <div className="text-sm text-muted">{greeting}</div>
            <h1 className="text-2xl font-semibold tracking-tight mt-0.5">
              {userName ? `${timeOfDayGreeting()}, ${userName}` : 'Today'}
            </h1>
          </div>
          <div className="flex items-center gap-2">
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

        {/* Stale brief banner */}
        {brief && briefIsStale && !briefLoading && (
          <button
            onClick={regenerateBrief}
            className="w-full flex items-center justify-between gap-3 px-4 py-2.5 rounded-xl border border-warn/40 bg-warn/10 text-warn hover:bg-warn/15 transition-colors text-sm"
          >
            <span className="inline-flex items-center gap-2">
              <Sparkles className="size-4" />
              Newer data available — your brief was generated before today's data landed
            </span>
            <span className="text-xs underline-offset-2 hover:underline">Regenerate</span>
          </button>
        )}

        {/* Key Takeaways */}
        {brief ? (
          <div className="space-y-3">
            <div className="text-xs font-medium uppercase tracking-wider text-muted">
              Key Takeaways
            </div>
            {brief.takeaways.map((t, i) => (
              <TakeawayCard key={i} takeaway={t} onAsk={() => askAbout(t)} />
            ))}
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
              <CardBody>
                <div className="overflow-x-auto -mx-5 px-5">
                  <table className="w-full text-sm">
                    <thead className="text-xs text-muted">
                      <tr className="text-left">
                        <th className="font-medium pb-2 pr-4">Date</th>
                        <th className="font-medium pb-2 pr-4">Type</th>
                        <th className="font-medium pb-2 pr-4 text-right">Distance</th>
                        <th className="font-medium pb-2 pr-4 text-right">Duration</th>
                        <th className="font-medium pb-2 pr-4 text-right">Pace</th>
                        <th className="font-medium pb-2 pr-4 text-right">HR</th>
                        <th className="font-medium pb-2 text-right">Load</th>
                      </tr>
                    </thead>
                    <tbody className="tabular-nums">
                      {workouts.map((w) => (
                        <tr key={w.activity_id} className="border-t border-border">
                          <td className="py-2 pr-4">{new Date(w.date).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}</td>
                          <td className="py-2 pr-4 text-muted capitalize">{w.activity_type.replace(/_/g, ' ')}</td>
                          <td className="py-2 pr-4 text-right">{fmtKm(w.distance_meters)}</td>
                          <td className="py-2 pr-4 text-right">{fmtSeconds(w.duration_seconds)}</td>
                          <td className="py-2 pr-4 text-right text-muted">{fmtPace(w.avg_pace_sec_per_km)}</td>
                          <td className="py-2 pr-4 text-right">{w.avg_hr ?? '—'}</td>
                          <td className="py-2 text-right text-accent">{w.training_load?.toFixed(0) ?? '—'}</td>
                        </tr>
                      ))}
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
