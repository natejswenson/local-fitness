import { useEffect, useMemo, useState } from 'react'
import {
  Bar, BarChart, CartesianGrid, ComposedChart, Line, ReferenceLine,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { Loader2 } from 'lucide-react'
import { api } from '@/lib/api'
import { Card, CardBody, CardHeader, CardTitle } from './Card'
import { DashboardInsight, type Prompt } from './DashboardInsight'
import { HeatmapDayTooltip, type HoverTarget, type LoadRanking } from './HeatmapDayTooltip'
import { fmtDate, fmtDateShort } from '@/lib/utils'
import type {
  ActivityHeatmapDay, PaceEfficiencyRun, StrengthVolumeWeek,
} from '@/lib/types'

type Model = 'sonnet' | 'opus'

/**
 * Custom dashboards page. Each view has an inline `<DashboardInsight />`
 * that streams the agent's answer directly under the chart that
 * prompted the question — no page-level chat panel, no scroll dance.
 * Session is shared across the three panels so context carries when
 * the user moves between them.
 */
export function Dashboards() {
  // One session for all three insights so the agent has continuity
  // when the user pivots between views. Generated once per page mount.
  const [sessionId] = useState(() => crypto.randomUUID())
  const [model, setModel] = useState<Model>('sonnet')

  // Tear down the chat session when the user leaves the page so the
  // server can release its agent client.
  useEffect(() => {
    return () => {
      api.chatEnd(sessionId).catch(() => {})
    }
  }, [sessionId])

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-6xl mx-auto px-6 py-8 space-y-6">
        <header className="flex items-end justify-between gap-3 flex-wrap">
          <div>
            <div className="text-sm text-muted">Custom views</div>
            <h1 className="text-2xl font-semibold tracking-tight mt-0.5">Dashboards</h1>
          </div>
          <ModelToggle value={model} onChange={setModel} />
        </header>

        <ActivityHeatmapPanel sessionId={sessionId} model={model} />
        <PaceEfficiencyPanel sessionId={sessionId} model={model} />
        <StrengthTrackerPanel sessionId={sessionId} model={model} />
      </div>
    </div>
  )
}

function ModelToggle({ value, onChange }: { value: Model; onChange: (v: Model) => void }) {
  return (
    <div className="flex bg-surface border border-border rounded-full p-0.5 text-[11px]">
      {(['sonnet', 'opus'] as const).map((m) => (
        <button
          key={m}
          onClick={() => onChange(m)}
          className={
            'px-2.5 py-1 rounded-full transition-colors ' +
            (value === m ? 'bg-accent text-bg' : 'text-muted hover:text-text')
          }
          title={m === 'sonnet' ? 'Sonnet 4.6 — fast' : 'Opus 4.7 — deeper reasoning'}
        >
          {m === 'sonnet' ? 'Sonnet' : 'Opus'}
        </button>
      ))}
    </div>
  )
}

function rangeLabel(days: number): string {
  if (days >= 365) return `${Math.round(days / 365)} year${days >= 730 ? 's' : ''}`
  if (days >= 30) return `${Math.round(days / 30)} months`
  return `${days} days`
}

/** Plain-English window descriptor for the rank line in the tooltip
 *  ("Hardest day this year" reads better than "in 1 year"). */
function rangeWindowLabel(days: number): string {
  if (days <= 90) return `in ${days} days`
  if (days <= 180) return 'in 6 months'
  if (days <= 365) return 'this year'
  if (days <= 730) return 'in 2 years'
  return `in ${days} days`
}

// =====================================================================
// Activity heatmap — calendar-style year view (52 weeks × 7 days)
// =====================================================================

const HEATMAP_RANGES = [
  { label: '90d', days: 90 },
  { label: '6mo', days: 180 },
  { label: '1y', days: 365 },
  { label: '2y', days: 730 },
] as const

function ActivityHeatmapPanel({ sessionId, model }: { sessionId: string; model: Model }) {
  const [days, setDays] = useState<number>(365)
  const [data, setData] = useState<ActivityHeatmapDay[] | null>(null)
  const [hover, setHover] = useState<HoverTarget | null>(null)

  useEffect(() => {
    setData(null)
    api.activityHeatmap(days).then((r) => setData(r.values))
  }, [days])

  // Active days only — rest days have 0 load and would skew the rank
  // toward "everyone is the hardest day."
  const ranking: LoadRanking | undefined = useMemo(() => {
    if (!data) return undefined
    const active = data.filter((d) => d.activity_count > 0)
    const sorted = [...active].sort((a, b) => b.total_load - a.total_load)
    const rankByDate = new Map<string, number>()
    sorted.forEach((d, i) => rankByDate.set(d.date, i + 1))
    return {
      rankByDate,
      totalActiveDays: active.length,
      windowLabel: rangeWindowLabel(days),
    }
  }, [data, days])

  const range = rangeLabel(days)
  const heatmapPrompts: Prompt[] = [
    {
      label: 'Spot overload weeks',
      seed: `Look at my activity heatmap for the last ${range}. Identify weeks where I overloaded — too many high-load days in a row without recovery — and compare them to weeks where I balanced load and rest well. What pattern should I aim for?`,
    },
    {
      label: 'Consistency vs spikes',
      seed: `Across the last ${range}, am I building up gradually or spiking? Highlight any sharp jumps in weekly load and whether they coincide with poor recovery markers (RHR, sleep, body battery).`,
    },
    {
      label: 'Days I should have rested',
      seed: `In the last ${range}, find days I trained hard when my recovery markers (RHR vs baseline, sleep score, body battery) suggested I should have rested. Be specific with dates.`,
    },
  ]

  return (
    <Card>
      <CardHeader>
        <div className="flex items-end justify-between gap-3 flex-wrap">
          <CardTitle>Activity heatmap</CardTitle>
          <RangeToggle
            options={HEATMAP_RANGES.map((r) => ({ label: r.label, value: r.days }))}
            value={days}
            onChange={setDays}
          />
        </div>
      </CardHeader>
      <CardBody>
        {data == null ? (
          <ChartLoading />
        ) : (
          <div className="space-y-3">
            <HeatmapGrid days={days} data={data} onHover={setHover} />
            <HeatmapTotals data={data} />
            <HeatmapDayTooltip target={hover} ranking={ranking} />
            <DashboardInsight
              prompts={heatmapPrompts}
              sessionId={sessionId}
              model={model}
              topic="activity heatmap"
            />
          </div>
        )}
      </CardBody>
    </Card>
  )
}

const MS_DAY = 86_400_000

function startOfDayUTC(d: Date): Date {
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()))
}

function HeatmapGrid({
  days, data, onHover,
}: {
  days: number
  data: ActivityHeatmapDay[]
  onHover: (target: HoverTarget | null) => void
}) {
  // Build the cell grid. Rows = day of week (0=Sun…6=Sat), cols = weeks.
  // Anchor on today (rightmost column) and walk back N days so the grid
  // ends exactly on today's column.
  const { weeks, maxLoad } = useMemo(() => {
    const byDate = new Map(data.map((d) => [d.date, d]))
    const today = startOfDayUTC(new Date())
    const earliestStart = startOfDayUTC(new Date(today.getTime() - (days - 1) * MS_DAY))
    // Round earliestStart down to the most recent Sunday so the first
    // column starts on a Sunday — keeps the grid aligned visually.
    const startWeek = new Date(earliestStart)
    startWeek.setUTCDate(startWeek.getUTCDate() - startWeek.getUTCDay())

    const weeks: { date: string; row: number; col: number; entry?: ActivityHeatmapDay }[] = []
    let col = 0
    let cursor = new Date(startWeek)
    while (cursor <= today) {
      for (let row = 0; row < 7; row++) {
        const cellDate = new Date(cursor.getTime() + row * MS_DAY)
        if (cellDate < earliestStart || cellDate > today) continue
        const iso = cellDate.toISOString().slice(0, 10)
        weeks.push({ date: iso, row, col, entry: byDate.get(iso) })
      }
      col++
      cursor = new Date(cursor.getTime() + 7 * MS_DAY)
    }
    const maxLoad = Math.max(50, ...data.map((d) => d.total_load))
    return { weeks, maxLoad }
  }, [data, days])

  const cellSize = 12
  const gap = 2
  const cols = Math.max(...weeks.map((w) => w.col)) + 1
  const widthPx = cols * (cellSize + gap)
  const heightPx = 7 * (cellSize + gap)

  return (
    <div className="overflow-x-auto">
      <svg
        width={widthPx}
        height={heightPx + 18}
        className="select-none"
        onMouseLeave={() => onHover(null)}
      >
        {/* Day-of-week labels — only Mon/Wed/Fri, faint */}
        {[1, 3, 5].map((dow) => (
          <text
            key={dow}
            x={-2}
            y={dow * (cellSize + gap) + cellSize - 1}
            fontSize="9"
            textAnchor="end"
            fill="var(--color-faint)"
          >
            {['', 'Mon', '', 'Wed', '', 'Fri', ''][dow]}
          </text>
        ))}
        {/* Cells */}
        {weeks.map((cell) => {
          // Rest days now arrive with entries too (active+rest spine) —
          // treat activity_count===0 as a rest cell so the visual stays
          // identical to the active-only days.
          const isActive = cell.entry && cell.entry.activity_count > 0
          const intensity = isActive
            ? Math.min(1, cell.entry!.total_load / maxLoad)
            : null
          const fill = intensity == null
            ? 'var(--color-surface-2)'
            : `oklch(${(0.32 + intensity * 0.42).toFixed(3)} ${(0.05 + intensity * 0.18).toFixed(3)} ${(155 - intensity * 130).toFixed(0)})`
          return (
            <rect
              key={cell.date}
              x={cell.col * (cellSize + gap)}
              y={cell.row * (cellSize + gap)}
              width={cellSize}
              height={cellSize}
              rx={2}
              fill={fill}
              stroke="var(--color-border)"
              strokeWidth={0.5}
              className="cursor-default hover:stroke-accent-dim"
              onMouseEnter={(e) => {
                const rect = (e.target as SVGRectElement).getBoundingClientRect()
                if (cell.entry && cell.entry.activity_count > 0) {
                  onHover({ kind: 'active', day: cell.entry, rect })
                } else {
                  // Rest day. The entry may be present (watch worn, no
                  // workout) carrying inline wellness/baseline/percentile,
                  // or absent (no daily_metrics row at all) → "no data".
                  onHover({ kind: 'rest', day: cell.entry ?? null, date: cell.date, rect })
                }
              }}
            />
          )
        })}
      </svg>
    </div>
  )
}

function HeatmapTotals({ data }: { data: ActivityHeatmapDay[] }) {
  // Spine is now daily_metrics (active + rest), so "active days" must
  // filter on activity_count rather than counting every row.
  const totals = useMemo(() => {
    const active = data.filter((d) => d.activity_count > 0)
    const days = active.length
    const totalLoad = active.reduce((s, d) => s + d.total_load, 0)
    const totalActivities = active.reduce((s, d) => s + d.activity_count, 0)
    return { days, totalLoad, totalActivities }
  }, [data])

  return (
    <div className="flex items-center justify-between text-xs text-muted gap-4 flex-wrap">
      <div className="tabular-nums">
        {totals.totalActivities} activities across {totals.days} active days · cumulative load {totals.totalLoad.toFixed(0)}
        <span className="text-faint ml-2">· hover any day for full stats</span>
      </div>
      <ScaleLegend />
    </div>
  )
}

function ScaleLegend() {
  return (
    <div className="inline-flex items-center gap-1.5 text-[10px] text-faint">
      <span>less</span>
      {[0, 0.25, 0.5, 0.75, 1].map((t) => (
        <span
          key={t}
          className="size-3 rounded-sm border border-border"
          style={{
            background: t === 0
              ? 'var(--color-surface-2)'
              : `oklch(${(0.32 + t * 0.42).toFixed(3)} ${(0.05 + t * 0.18).toFixed(3)} ${(155 - t * 130).toFixed(0)})`,
          }}
        />
      ))}
      <span>more</span>
    </div>
  )
}

// =====================================================================
// Pace efficiency — HR-per-kmh trend with TSB overlay
// =====================================================================

const PACE_RANGES = [
  { label: '90d', days: 90 },
  { label: '6mo', days: 180 },
  { label: '1y', days: 365 },
  { label: '2y', days: 730 },
] as const

function PaceEfficiencyPanel({ sessionId, model }: { sessionId: string; model: Model }) {
  const [days, setDays] = useState<number>(180)
  const [runs, setRuns] = useState<PaceEfficiencyRun[] | null>(null)

  useEffect(() => {
    setRuns(null)
    api.paceEfficiency(days, 2).then((r) => setRuns(r.values))
  }, [days])

  const range = rangeLabel(days)
  const pacePrompts: Prompt[] = [
    {
      label: 'Read the trend',
      seed: `Walk me through my pace efficiency (HR per km/h) trend over the last ${range}. Is it improving or worsening? Cite the specific runs and the rolling-average shape.`,
    },
    {
      label: 'Fatigue signals',
      seed: `In the last ${range}, identify runs where my HR was disproportionately high relative to pace — the divergence-from-baseline runs. Cross-check with TSB on those dates: when negative, was the high HR/pace expected, or genuine fatigue?`,
    },
    {
      label: 'Detraining vs fitness',
      seed: `Looking at HR/pace efficiency over the last ${range} alongside CTL: am I gaining fitness, holding, or losing it? Distinguish between brief fatigue dips and a real downward trend.`,
    },
    {
      label: 'Best efficiency runs',
      seed: `What were my 5 most efficient runs (lowest HR per km/h) in the last ${range}? What did the recovery context look like in the days before each?`,
    },
  ]

  // Rolling 5-run average of hr_per_kmh — smooths noise so the trend is
  // visible without losing per-run dots. TSB stays on its own axis.
  const smoothed = useMemo(() => {
    if (!runs) return null
    const out: { date: string; hr_per_kmh: number; rolling: number | null; tsb: number | null }[] = []
    for (let i = 0; i < runs.length; i++) {
      const r = runs[i]
      if (r.hr_per_kmh == null) continue
      const window = runs.slice(Math.max(0, i - 4), i + 1)
        .map((x) => x.hr_per_kmh)
        .filter((v): v is number => v != null)
      const rolling = window.length >= 3
        ? window.reduce((s, v) => s + v, 0) / window.length
        : null
      out.push({
        date: r.date,
        hr_per_kmh: r.hr_per_kmh,
        rolling,
        tsb: r.tsb,
      })
    }
    return out
  }, [runs])

  return (
    <Card>
      <CardHeader>
        <div className="flex items-end justify-between gap-3 flex-wrap">
          <div>
            <CardTitle>Pace efficiency &amp; fatigue</CardTitle>
            <p className="text-[11px] text-faint mt-1">
              HR per km/h on each run. Rising line = more cardiovascular cost for
              the same speed (fatigue / detraining). TSB overlay shows whether
              the rise tracks intentional load.
            </p>
          </div>
          <RangeToggle
            options={PACE_RANGES.map((r) => ({ label: r.label, value: r.days }))}
            value={days}
            onChange={setDays}
          />
        </div>
      </CardHeader>
      <CardBody>
        <div className="h-80">
          {smoothed == null ? (
            <ChartLoading />
          ) : smoothed.length === 0 ? (
            <EmptyState>
              No runs in this window with both HR and pace recorded.
            </EmptyState>
          ) : (
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={smoothed} margin={{ top: 8, right: 24, left: 8, bottom: 0 }}>
                <CartesianGrid stroke="var(--color-border)" strokeDasharray="2 4" vertical={false} />
                <XAxis
                  dataKey="date"
                  tick={{ fill: 'var(--color-faint)', fontSize: 11 }}
                  tickFormatter={fmtDateShort}
                  axisLine={{ stroke: 'var(--color-border)' }}
                  tickLine={false}
                  minTickGap={50}
                />
                <YAxis
                  yAxisId="hr"
                  tick={{ fill: 'var(--color-faint)', fontSize: 11 }}
                  axisLine={false}
                  tickLine={false}
                  width={36}
                  domain={['auto', 'auto']}
                />
                <YAxis
                  yAxisId="tsb"
                  orientation="right"
                  tick={{ fill: 'var(--color-faint)', fontSize: 11 }}
                  axisLine={false}
                  tickLine={false}
                  width={32}
                  domain={['auto', 'auto']}
                />
                <Tooltip content={<PaceTooltip />} />
                <ReferenceLine yAxisId="tsb" y={0} stroke="var(--color-border)" />
                {/* per-run dots */}
                <Line
                  yAxisId="hr"
                  dataKey="hr_per_kmh"
                  stroke="oklch(0.78 0.16 28)"
                  strokeWidth={0}
                  dot={{ r: 2.5, fill: 'oklch(0.78 0.16 28)', strokeWidth: 0 }}
                  name="Per-run HR/km·h"
                  isAnimationActive={false}
                />
                {/* rolling smoother */}
                <Line
                  yAxisId="hr"
                  dataKey="rolling"
                  stroke="oklch(0.78 0.16 28)"
                  strokeWidth={2}
                  dot={false}
                  type="monotone"
                  name="5-run rolling"
                  isAnimationActive={false}
                />
                {/* TSB on right axis */}
                <Line
                  yAxisId="tsb"
                  dataKey="tsb"
                  stroke="oklch(0.78 0.16 158)"
                  strokeWidth={1.6}
                  strokeDasharray="3 3"
                  dot={false}
                  type="monotone"
                  name="TSB"
                  isAnimationActive={false}
                />
              </ComposedChart>
            </ResponsiveContainer>
          )}
        </div>
        <div className="flex gap-4 text-[11px] text-muted mt-3">
          <Legend color="oklch(0.78 0.16 28)" label="HR per km/h (lower is better)" />
          <Legend color="oklch(0.78 0.16 158)" label="TSB — negative = accumulated fatigue" />
        </div>
        <DashboardInsight
          prompts={pacePrompts}
          sessionId={sessionId}
          model={model}
          topic="pace efficiency"
        />
      </CardBody>
    </Card>
  )
}

function PaceTooltip({
  active, payload, label,
}: {
  active?: boolean
  payload?: { name: string; value: number; color: string; dataKey: string }[]
  label?: string
}) {
  if (!active || !payload?.length) return null
  return (
    <div className="bg-surface-2 border border-border rounded-lg px-3 py-2 text-xs shadow-elev tabular-nums">
      <div className="text-muted mb-1">{label && fmtDateShort(label)}</div>
      {payload.map((p) => (
        <div key={p.dataKey} className="flex items-center gap-2">
          <span className="size-2 rounded-full" style={{ background: p.color }} />
          <span className="text-muted">{p.name}:</span>
          <span className="text-text font-medium">
            {p.value == null ? '—' : p.value.toFixed(2)}
          </span>
        </div>
      ))}
    </div>
  )
}

// =====================================================================
// Strength tracker — weekly session count + total load
// =====================================================================

const STRENGTH_RANGES = [
  { label: '1y', weeks: 52 },
  { label: '2y', weeks: 104 },
  { label: '5y', weeks: 260 },
] as const

function StrengthTrackerPanel({ sessionId, model }: { sessionId: string; model: Model }) {
  const [weeks, setWeeks] = useState<number>(104)
  const [resp, setResp] = useState<{
    values: StrengthVolumeWeek[]
    last_session_date: string | null
    total_sessions: number
  } | null>(null)

  useEffect(() => {
    setResp(null)
    api.strengthVolume(weeks).then((r) => setResp({
      values: r.values,
      last_session_date: r.last_session_date,
      total_sessions: r.total_sessions,
    }))
  }, [weeks])

  const range = `${weeks} weeks`
  const strengthPrompts: Prompt[] = [
    {
      label: 'Why so little strength?',
      seed: `My strength training has fallen off — last logged ${resp?.last_session_date ?? 'a long time ago'}. Walk me through whether this is hurting my running performance and recovery, and what the minimum viable strength routine would be for someone with my running load.`,
    },
    {
      label: 'Complement my running',
      seed: `Given my recent running volume and the lack of strength sessions in the last ${range}, what 2-3 specific strength movements (lifts, plyometrics, mobility) would most directly improve my running and reduce injury risk? Be concrete — sets, reps, weight if applicable, and frequency.`,
    },
    {
      label: 'Restart plan',
      seed: `Build me a 4-week ramp to restart strength training without compromising my current running schedule. Account for my CTL/ATL trajectory and recommend specific session days based on my typical running pattern.`,
    },
  ]

  return (
    <Card>
      <CardHeader>
        <div className="flex items-end justify-between gap-3 flex-wrap">
          <div>
            <CardTitle>Strength volume</CardTitle>
            <p className="text-[11px] text-faint mt-1">
              Weekly count of strength-tagged sessions. Instinct Solar doesn't
              record sets/reps/weight, so this tracks frequency + duration only.
            </p>
          </div>
          <RangeToggle
            options={STRENGTH_RANGES.map((r) => ({ label: r.label, value: r.weeks }))}
            value={weeks}
            onChange={setWeeks}
          />
        </div>
      </CardHeader>
      <CardBody>
        {resp == null ? (
          <ChartLoading />
        ) : resp.total_sessions === 0 ? (
          <EmptyState>
            No strength sessions recorded in this window. Log them on your
            watch as a "Strength" activity to start populating this view.
          </EmptyState>
        ) : (
          <>
            <div className="h-64">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={resp.values} margin={{ top: 8, right: 8, left: 8, bottom: 0 }}>
                  <CartesianGrid stroke="var(--color-border)" strokeDasharray="2 4" vertical={false} />
                  <XAxis
                    dataKey="week_start"
                    tick={{ fill: 'var(--color-faint)', fontSize: 11 }}
                    tickFormatter={fmtDateShort}
                    axisLine={{ stroke: 'var(--color-border)' }}
                    tickLine={false}
                    minTickGap={40}
                  />
                  <YAxis
                    tick={{ fill: 'var(--color-faint)', fontSize: 11 }}
                    axisLine={false}
                    tickLine={false}
                    width={28}
                    allowDecimals={false}
                  />
                  <Tooltip content={<StrengthTooltip />} />
                  <Bar
                    dataKey="sessions"
                    fill="oklch(0.65 0.16 280)"
                    radius={[3, 3, 0, 0]}
                    isAnimationActive={false}
                  />
                </BarChart>
              </ResponsiveContainer>
            </div>
            <div className="flex justify-between text-xs text-muted mt-3 tabular-nums">
              <div>{resp.total_sessions} session{resp.total_sessions === 1 ? '' : 's'} total</div>
              <div>
                Last: {resp.last_session_date ? fmtDate(resp.last_session_date) : '—'}
              </div>
            </div>
          </>
        )}
        <DashboardInsight
          prompts={strengthPrompts}
          sessionId={sessionId}
          model={model}
          topic="strength"
        />
      </CardBody>
    </Card>
  )
}

function StrengthTooltip({
  active, payload, label,
}: {
  active?: boolean
  payload?: { payload: StrengthVolumeWeek }[]
  label?: string
}) {
  if (!active || !payload?.length) return null
  const w = payload[0].payload
  return (
    <div className="bg-surface-2 border border-border rounded-lg px-3 py-2 text-xs shadow-elev tabular-nums">
      <div className="text-muted mb-1">Week of {label && fmtDateShort(label)}</div>
      <div className="flex items-center gap-2">
        <span className="text-muted">sessions:</span>
        <span className="text-text font-medium">{w.sessions}</span>
      </div>
      <div className="flex items-center gap-2">
        <span className="text-muted">duration:</span>
        <span className="text-text font-medium">{w.total_duration_min.toFixed(0)} min</span>
      </div>
      <div className="flex items-center gap-2">
        <span className="text-muted">calories:</span>
        <span className="text-text font-medium">{w.total_calories}</span>
      </div>
    </div>
  )
}

// =====================================================================
// Shared bits
// =====================================================================

function RangeToggle<T extends number>({
  options, value, onChange,
}: {
  options: { label: string; value: T }[]
  value: T
  onChange: (v: T) => void
}) {
  return (
    <div className="flex bg-surface border border-border rounded-full p-0.5 text-xs">
      {options.map((opt) => (
        <button
          key={opt.value}
          onClick={() => onChange(opt.value)}
          className={
            'px-3 py-1.5 rounded-full transition-colors ' +
            (value === opt.value
              ? 'bg-accent text-bg'
              : 'text-muted hover:text-text')
          }
        >
          {opt.label}
        </button>
      ))}
    </div>
  )
}

function ChartLoading() {
  return (
    <div className="h-64 flex items-center justify-center">
      <Loader2 className="size-4 text-muted animate-spin" />
    </div>
  )
}

function EmptyState({ children }: { children: React.ReactNode }) {
  return (
    <div className="h-40 flex items-center justify-center text-sm text-muted text-center px-6">
      {children}
    </div>
  )
}

function Legend({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="size-2 rounded-full" style={{ background: color }} />
      {label}
    </span>
  )
}

