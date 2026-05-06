export type DailyMetric = {
  date: string
  sleep_seconds: number | null
  sleep_score: number | null
  rhr: number | null
  avg_stress: number | null
  max_stress: number | null
  body_battery_min: number | null
  body_battery_max: number | null
  steps: number | null
  training_status: string | null
  intensity_minutes_moderate: number | null
  intensity_minutes_vigorous: number | null
}

export type Baseline = {
  date: string
  rhr_60day_mean: number | null
  rhr_60day_sd: number | null
  body_battery_max_60day_mean: number | null
  body_battery_min_60day_mean: number | null
  sleep_seconds_60day_mean: number | null
  sleep_seconds_60day_sd: number | null
  stress_60day_mean: number | null
  ctl: number | null
  atl: number | null
  tsb: number | null
}

export type TodayResponse = {
  today: string
  latest: DailyMetric & { vo2_max: number | null; respiration_avg: number | null; floors_climbed: number | null } | null
  recent_14d: DailyMetric[]
  baseline: Baseline | null
}

export type MetricSeries = {
  metric: string
  days: number
  values: { date: string; value: number }[]
  baseline: { date: string; value: number }[] | null
}

export type TrainingLoadSeries = {
  days: number
  values: { date: string; ctl: number; atl: number; tsb: number }[]
}

export type Workout = {
  activity_id: number
  date: string
  start_time: string
  activity_type: string
  activity_name: string
  duration_seconds: number | null
  distance_meters: number | null
  avg_hr: number | null
  max_hr: number | null
  avg_pace_sec_per_km: number | null
  elevation_gain_meters: number | null
  aerobic_te: number | null
  anaerobic_te: number | null
  training_load: number | null
}

export type ChatEvent =
  | { type: 'text'; text: string }
  | { type: 'tool_use'; name: string; input: Record<string, unknown> }
  | { type: 'thinking'; text: string }
  | { type: 'done' }
  | { type: 'error'; message: string }

export type BriefStreamEvent =
  | { type: 'takeaway'; index: number; takeaway: Takeaway }
  | { type: 'done'; brief: Brief; data_through_date: string | null }
  | { type: 'error'; message: string }

// ---- Brief (structured Takeaways) ----

export type TakeawayTone = 'positive' | 'caution' | 'critical' | 'neutral'

export type TakeawayMetricRef = {
  metric: string
  days: number
}

export type Takeaway = {
  headline: string
  summary: string
  tone: TakeawayTone
  metric: TakeawayMetricRef | null
  details: string
}

export type Brief = {
  date: string
  user_name: string
  generated_at: string | null
  takeaways: Takeaway[]
}

export type BriefResponse = {
  date: string
  brief: Brief | null
  cached: boolean
  data_through_date: string | null
}

// ---- Sync ----

export type SyncStatus =
  | 'success'
  | 'skipped'
  | 'partial'
  | 'failure'
  | 'auth_failure'
  | 'not_configured'
  | 'in_progress'
  | 'orphaned'
  | 'interrupted'
  | null

export type SyncState = {
  is_running: boolean
  started_at: string | null
  last_status: SyncStatus
  last_completed_at: string | null
  last_date_fetched: string | null
  last_error: string | null
  throttle_seconds: number
  next_eligible_at: string | null
  seconds_until_eligible: number
  max_days_per_pull: number
  data_through_date: string | null
  days_behind: number
}

export type SyncTriggerResponse = {
  started: boolean
  reason?: 'already_running' | 'throttled'
  state: SyncState
}

// ---- Custom dashboards ----

export type HeatmapWellness = {
  rhr: number | null
  sleep_seconds: number | null
  sleep_score: number | null
  body_battery_max: number | null
  body_battery_min: number | null
  avg_stress: number | null
  steps: number | null
}

export type HeatmapBaseline = {
  rhr_60d: number | null
  sleep_seconds_60d: number | null
  body_battery_max_60d: number | null
  stress_60d: number | null
}

export type HeatmapLoadState = {
  ctl: number | null
  atl: number | null
  tsb: number | null
}

export type HeatmapActivity = {
  activity_id: number
  type: string
  name: string
  duration_seconds: number | null
  distance_meters: number | null
  training_load: number | null
  avg_hr: number | null
  max_hr: number | null
  avg_pace_sec_per_km: number | null
}

/**
 * Per-marker percentile rank within the visible window. 0 = best for
 * that metric (lowest RHR, longest sleep, highest body battery, lowest
 * stress); 100 = worst. Computed across ALL daily_metrics rows in the
 * window, including rest days.
 */
export type HeatmapRecoveryPct = {
  rhr: number | null
  sleep_seconds: number | null
  body_battery_max: number | null
  avg_stress: number | null
}

export type ActivityHeatmapDay = {
  date: string
  /** 0 for rest days, > 0 for active. */
  activity_count: number
  total_load: number
  total_duration_seconds: number
  dominant_type: string | null
  activities: HeatmapActivity[]
  wellness: HeatmapWellness
  baseline: HeatmapBaseline
  load_state: HeatmapLoadState
  recovery_pct: HeatmapRecoveryPct
}

export type ActivityHeatmapResponse = {
  days: number
  start_date: string
  end_date: string
  values: ActivityHeatmapDay[]
}

export type StrengthVolumeWeek = {
  iso_week: string
  week_start: string
  sessions: number
  total_duration_min: number
  total_load: number
  total_calories: number
}

export type StrengthVolumeResponse = {
  weeks: number
  start_date: string
  end_date: string
  values: StrengthVolumeWeek[]
  last_session_date: string | null
  total_sessions: number
}

export type PaceEfficiencyRun = {
  date: string
  start_time: string
  activity_type: string
  activity_name: string
  avg_hr: number
  avg_pace_sec_per_km: number
  distance_meters: number
  duration_seconds: number
  training_load: number | null
  hr_per_kmh: number | null
  tsb: number | null
  ctl: number | null
  atl: number | null
}

export type PaceEfficiencyResponse = {
  days: number
  min_distance_km: number
  start_date: string
  end_date: string
  values: PaceEfficiencyRun[]
}
