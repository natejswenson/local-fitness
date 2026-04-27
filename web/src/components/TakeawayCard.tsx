import { useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { AlertTriangle, ChevronDown, ChevronRight, Info, MessageSquarePlus, Sparkles, TrendingDown } from 'lucide-react'
import { api } from '@/lib/api'
import type { Takeaway, TakeawayTone } from '@/lib/types'
import { Card } from './Card'
import { cn, fmtDateShort } from '@/lib/utils'

const TONE_STYLE: Record<TakeawayTone, {
  border: string; iconBg: string; iconColor: string; chartColor: string; Icon: typeof Sparkles
}> = {
  positive: {
    border: 'border-l-good/70',
    iconBg: 'bg-good/15',
    iconColor: 'text-good',
    chartColor: 'oklch(0.78 0.16 158)',
    Icon: Sparkles,
  },
  caution: {
    border: 'border-l-warn/70',
    iconBg: 'bg-warn/15',
    iconColor: 'text-warn',
    chartColor: 'oklch(0.78 0.16 65)',
    Icon: AlertTriangle,
  },
  critical: {
    border: 'border-l-bad/70',
    iconBg: 'bg-bad/15',
    iconColor: 'text-bad',
    chartColor: 'oklch(0.65 0.20 27)',
    Icon: TrendingDown,
  },
  neutral: {
    border: 'border-l-border',
    iconBg: 'bg-surface-2',
    iconColor: 'text-muted',
    chartColor: 'oklch(0.62 0.01 240)',
    Icon: Info,
  },
}

const METRIC_LABELS: Record<string, { label: string; unit?: string; transform?: (v: number) => number }> = {
  rhr: { label: 'Resting HR', unit: 'bpm' },
  sleep_seconds: { label: 'Sleep', unit: 'h', transform: (v) => v / 3600 },
  sleep_score: { label: 'Sleep score' },
  body_battery_max: { label: 'Body Battery (peak)' },
  body_battery_min: { label: 'Body Battery (low)' },
  avg_stress: { label: 'Stress (avg)' },
  vo2_max: { label: 'VO₂ max' },
  steps: { label: 'Steps' },
  intensity_minutes_moderate: { label: 'Moderate intensity', unit: 'min' },
  intensity_minutes_vigorous: { label: 'Vigorous intensity', unit: 'min' },
  ctl: { label: 'Fitness (CTL)' },
  atl: { label: 'Fatigue (ATL)' },
  tsb: { label: 'Freshness (TSB)' },
}

export function TakeawayCard({
  takeaway, onAsk,
}: {
  takeaway: Takeaway
  onAsk?: () => void
}) {
  const [expanded, setExpanded] = useState(false)
  const tone = TONE_STYLE[takeaway.tone]
  const Icon = tone.Icon

  return (
    <Card className={cn('border-l-4', tone.border)}>
      <div className="px-5 pt-4 pb-2 flex items-start gap-3">
        <div className={cn('size-8 rounded-lg flex items-center justify-center shrink-0', tone.iconBg)}>
          <Icon className={cn('size-4', tone.iconColor)} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="text-base font-semibold tracking-tight leading-snug text-text">
            {takeaway.headline}
          </div>
          <div className="text-sm text-muted mt-1">{takeaway.summary}</div>
        </div>
      </div>

      {takeaway.metric && (
        <div className="px-5 pt-2">
          <MetricChart
            metricName={takeaway.metric.metric}
            days={takeaway.metric.days}
            color={tone.chartColor}
          />
        </div>
      )}

      <div className="flex items-stretch border-t border-border mt-1">
        <button
          onClick={() => setExpanded((v) => !v)}
          className="flex-1 px-5 py-2.5 flex items-center gap-1.5 text-xs text-muted hover:text-text transition-colors"
        >
          {expanded ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
          {expanded ? 'Less' : 'More'}
        </button>
        {onAsk && (
          <button
            onClick={onAsk}
            className="px-4 py-2.5 flex items-center gap-1.5 text-xs text-muted hover:text-accent transition-colors border-l border-border"
            title="Ask the agent about this takeaway"
          >
            <MessageSquarePlus className="size-3.5" />
            Ask about this
          </button>
        )}
      </div>

      {expanded && (
        <div className="px-5 pb-5 prose-fitness text-[14.5px]">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{takeaway.details}</ReactMarkdown>
        </div>
      )}
    </Card>
  )
}

function MetricChart({ metricName, days, color }: { metricName: string; days: number; color: string }) {
  const meta = METRIC_LABELS[metricName] ?? { label: metricName }
  const [series, setSeries] = useState<{ date: string; value: number }[] | null>(null)

  useEffect(() => {
    let cancelled = false
    const isLoad = metricName === 'ctl' || metricName === 'atl' || metricName === 'tsb'
    const fetcher = isLoad
      ? api.trainingLoad(days).then((r) => r.values.map((v) => ({
          date: v.date,
          value: v[metricName as 'ctl' | 'atl' | 'tsb'],
        })))
      : api.metric(metricName, days).then((r) => r.values.map((v) => ({
          date: v.date,
          value: meta.transform ? meta.transform(v.value) : v.value,
        })))
    fetcher.then((s) => { if (!cancelled) setSeries(s) })
    return () => { cancelled = true }
  }, [metricName, days, meta])

  const current = series?.length ? series[series.length - 1].value : null
  const formatValue = (v: number) =>
    meta.unit === 'h' ? `${v.toFixed(1)}h`
    : Number.isInteger(v) ? `${v}${meta.unit ? ' ' + meta.unit : ''}`
    : `${v.toFixed(1)}${meta.unit ? ' ' + meta.unit : ''}`

  return (
    <div className="bg-bg/40 border border-border/60 rounded-lg overflow-hidden">
      <div className="px-3 pt-2 pb-1 flex items-baseline justify-between">
        <span className="text-[11px] font-medium uppercase tracking-wider text-muted">
          {meta.label} · {days}d
        </span>
        {current != null && (
          <span className="text-sm tabular-nums text-text font-medium">
            {formatValue(current)}
          </span>
        )}
      </div>
      <div className="h-24">
        {series == null ? null : (
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={series} margin={{ top: 4, right: 8, left: 8, bottom: 0 }}>
              <defs>
                <linearGradient id={`tk-${metricName}-${days}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={color} stopOpacity={0.4} />
                  <stop offset="100%" stopColor={color} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="var(--color-border)" strokeDasharray="2 4" vertical={false} />
              <XAxis
                dataKey="date"
                tick={{ fill: 'var(--color-faint)', fontSize: 10 }}
                tickFormatter={fmtDateShort}
                axisLine={false}
                tickLine={false}
                minTickGap={40}
              />
              <YAxis
                tick={{ fill: 'var(--color-faint)', fontSize: 10 }}
                axisLine={false}
                tickLine={false}
                width={28}
                domain={['auto', 'auto']}
              />
              <Tooltip content={<MiniTooltip formatter={formatValue} />} />
              <Area
                dataKey="value"
                stroke={color}
                strokeWidth={1.6}
                fill={`url(#tk-${metricName}-${days})`}
                isAnimationActive={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  )
}

function MiniTooltip({
  active, payload, label, formatter,
}: {
  active?: boolean
  payload?: { value: number }[]
  label?: string
  formatter: (v: number) => string
}) {
  if (!active || !payload?.length) return null
  return (
    <div className="bg-surface-2 border border-border rounded-md px-2 py-1 text-[11px] shadow-elev tabular-nums">
      <span className="text-muted">{label && fmtDateShort(label)}</span>
      <span className="text-text font-medium ml-2">{formatter(payload[0].value)}</span>
    </div>
  )
}
