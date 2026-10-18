// Canonical time/date formatting. Shared `Intl` instances (created once, not
// per-render) + relative-time helpers. Every surface that shows a timestamp or
// an age pulls from here so the rendered strings stay consistent app-wide.

export const SECOND = 1000
export const MINUTE = 60_000
export const HOUR = 3_600_000
export const DAY = 86_400_000

// ── Absolute date/time formatters ──────────────────────────────────────────
// `hh:mm` clock (thread today/yesterday lines).
export const fmtClock = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' })

// Compact "day + clock", no year/seconds (artifacts, thread fallback, cron runs).
export const fmtDayTime = new Intl.DateTimeFormat(undefined, {
  day: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
  month: 'short'
})

// Medium date + short time (command center session detail).
export const fmtDateTime = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' })

// Weekday + full date + clock (message time tooltips).
export const fmtFullDateTime = new Intl.DateTimeFormat(undefined, { dateStyle: 'full', timeStyle: 'short' })

// Date only, "5 Jun 2026" (starmap tooltip).
export const fmtDate = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short', year: 'numeric' })

// Month name alone / with year — session-list date-bucket dividers ("September",
// "September 2025").
export const fmtMonth = new Intl.DateTimeFormat(undefined, { month: 'long' })
export const fmtMonthYear = new Intl.DateTimeFormat(undefined, { month: 'long', year: 'numeric' })

// ── Trustworthy epoch seconds ──────────────────────────────────────────────
// Epoch-seconds window a stored timestamp must fall in to be trusted: 1970 .. ~2103.
// These mirror `EPOCH_MIN`/`EPOCH_MAX` in `hermes_cli/timefmt.py` (`coerce_epoch`) so the
// desktop and the backend agree on what "trustworthy" means. SQLite's dynamic typing lets a
// TEXT cell, `inf`/`nan`, or a garbage double salvaged from a damaged page (`5.49e+246`) sit in
// a REAL column; `new Date(garbage * 1000)` is then an Invalid Date whose `.toISOString()`
// throws `RangeError: Invalid time value`, which took the whole Sessions pane down rather than
// degrading one row to an unknown age (#91536).
export const EPOCH_MIN_SECONDS = 0
export const EPOCH_MAX_SECONDS = 4_200_000_000

/**
 * The first candidate that is a trustworthy Unix-seconds timestamp, else `null`.
 *
 * Takes its candidates in preference order — `trustedEpochSeconds(session.last_active,
 * session.started_at)` — so an unset or corrupt `last_active` falls through to `started_at`
 * instead of blanking the row. A falsy candidate (`0`, `NaN`, `null`, `undefined`) is
 * "unset" and skipped, exactly as the `x || y` idiom this replaces treated it; a TRUTHY
 * but out-of-window candidate is "corrupt" and is likewise skipped, which is the gap that
 * let a salvaged double through: it is truthy and finite, so it sailed straight into
 * `new Date(...)` (#91536).
 */
export function trustedEpochSeconds(...candidates: readonly unknown[]): number | null {
  for (const candidate of candidates) {
    // Falsy first: matches `||`, so 0/unset keeps falling through to the next candidate
    // rather than being accepted as a real (1970) timestamp.
    if (!candidate || typeof candidate !== 'number') {
      continue
    }

    // Also false for NaN, which the window comparison below would otherwise miss.
    if (Number.isFinite(candidate) && candidate >= EPOCH_MIN_SECONDS && candidate <= EPOCH_MAX_SECONDS) {
      return candidate
    }
  }

  return null
}

// ── Relative time ──────────────────────────────────────────────────────────
const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto', style: 'short' })

// Localized bidirectional "in 5 min" / "2 hr ago" — coarsest sensible unit so a
// daily job reads "in 14 hr", not "in 840 min".
export function relativeTime(targetMs: number, nowMs = Date.now()): string {
  const diff = targetMs - nowMs
  const abs = Math.abs(diff)
  const sign = diff < 0 ? -1 : 1

  if (abs < MINUTE) {
    return rtf.format(sign * Math.round(abs / SECOND), 'second')
  }

  if (abs < HOUR) {
    return rtf.format(sign * Math.round(abs / MINUTE), 'minute')
  }

  if (abs < DAY) {
    return rtf.format(sign * Math.round(abs / HOUR), 'hour')
  }

  return rtf.format(sign * Math.round(abs / DAY), 'day')
}

// A dated divider bucket below the sidebar's unlabelled "recent" head cluster
// (see session-date-groups.ts for the clustering). Buckets are coarse,
// non-overlapping calendar ranges — one divider per *cluster* of activity,
// never one per day, and never a rolling window like "previous 7 days" that
// semantically overlaps the groups above it. `kind` drives the label; `at` is
// the session's nominal day start (ms) for month formatting.
export type SessionBucketKind = 'lastWeek' | 'month' | 'monthYear' | 'thisMonth' | 'thisWeek' | 'today' | 'yesterday'

export interface SessionBucket {
  at: number
  key: string
  kind: SessionBucketKind
}

// Fixed divider labels, resolved from i18n (month labels come from Intl).
export interface SessionBucketLabels {
  lastWeek: string
  thisMonth: string
  thisWeek: string
  today: string
  yesterday: string
}

export const startOfLocalDay = (ms: number): number => {
  const d = new Date(ms)

  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()
}

// The human day doesn't end at midnight — it ends when you sleep. Sessions
// from the small hours belong to the previous evening's run, so the day
// boundary sits at 4 AM local (same trick activity/sleep trackers use).
// A 12:30 AM session groups with 11:50 PM instead of splitting off.
export const DAY_ROLLOVER_HOUR = 4

// Start of the *nominal* local day a timestamp belongs to, honoring the 4 AM
// rollover: Saturday 1 AM → start of Friday.
export const nominalDayStart = (ms: number): number => startOfLocalDay(ms - DAY_ROLLOVER_HOUR * HOUR)

// Locale-aware first day of week in JS getDay() convention (0=Sun … 6=Sat).
// Intl.Locale weekInfo reports 1=Mon … 7=Sun; unsupported → Monday.
export function localeWeekStartDay(): number {
  try {
    const locale = new Intl.Locale(new Intl.DateTimeFormat().resolvedOptions().locale)
    const withWeekInfo = locale as { getWeekInfo?: () => { firstDay?: number }; weekInfo?: { firstDay?: number } }
    const firstDay = (withWeekInfo.getWeekInfo?.() ?? withWeekInfo.weekInfo)?.firstDay

    return typeof firstDay === 'number' ? firstDay % 7 : 1
  } catch {
    return 1
  }
}

// Start of the local calendar week containing `ms` (DST-safe Date field math).
export function startOfLocalWeek(ms: number, weekStartsOn: number): number {
  const d = new Date(startOfLocalDay(ms))
  const back = (d.getDay() - weekStartsOn + 7) % 7

  return new Date(d.getFullYear(), d.getMonth(), d.getDate() - back).getTime()
}

// Coarse calendar bucket for a Unix-seconds timestamp. Granularity coarsens
// with age: earlier today → yesterday → earlier this week → last week →
// earlier this month → month → month + year. Empty ranges simply never emit a
// bucket, so a sparse tail jumps straight to its month or month-year. The
// newest run of sessions never reaches here (it is the unlabelled head — see
// session-date-groups.ts), which is what makes "Earlier today" truthful.
export function calendarBucket(
  seconds: number,
  nowMs = Date.now(),
  weekStartsOn = localeWeekStartDay()
): SessionBucket {
  const nominal = nominalDayStart(seconds * SECOND)
  const todayNominal = nominalDayStart(nowMs)
  const dayDiff = Math.round((todayNominal - nominal) / DAY)

  if (dayDiff <= 0) {
    return { at: nominal, key: 'today', kind: 'today' }
  }

  if (dayDiff === 1) {
    return { at: nominal, key: 'yesterday', kind: 'yesterday' }
  }

  const weekStart = startOfLocalWeek(todayNominal, weekStartsOn)

  if (nominal >= weekStart) {
    return { at: nominal, key: 'this-week', kind: 'thisWeek' }
  }

  const ws = new Date(weekStart)

  if (nominal >= new Date(ws.getFullYear(), ws.getMonth(), ws.getDate() - 7).getTime()) {
    return { at: nominal, key: 'last-week', kind: 'lastWeek' }
  }

  const d = new Date(nominal)
  const now = new Date(todayNominal)
  const sameYear = d.getFullYear() === now.getFullYear()

  if (sameYear && d.getMonth() === now.getMonth()) {
    return { at: nominal, key: 'this-month', kind: 'thisMonth' }
  }

  const ym = `${d.getFullYear()}-${d.getMonth()}`

  return sameYear ? { at: nominal, key: `m-${ym}`, kind: 'month' } : { at: nominal, key: `my-${ym}`, kind: 'monthYear' }
}

// Localized divider label for a bucket: fixed relative strings from i18n,
// Intl-formatted month / month-year for the rest.
export function sessionBucketLabel(bucket: SessionBucket, labels: SessionBucketLabels): string {
  switch (bucket.kind) {
    case 'today':
      return labels.today

    case 'yesterday':
      return labels.yesterday

    case 'thisWeek':
      return labels.thisWeek

    case 'lastWeek':
      return labels.lastWeek

    case 'thisMonth':
      return labels.thisMonth

    case 'month':
      return fmtMonth.format(bucket.at)

    case 'monthYear':
      return fmtMonthYear.format(bucket.at)
  }
}

export type ElapsedUnit = 'day' | 'hour' | 'minute' | 'second'

// Coarsest elapsed bucket for a (clamped-nonnegative) duration, floored. The
// caller owns rendering — compact "5m", "5m ago", etc. — so no format is baked
// in here.
export function coarseElapsed(deltaMs: number): { unit: ElapsedUnit; value: number } {
  const ms = Math.max(0, deltaMs)

  if (ms >= DAY) {
    return { unit: 'day', value: Math.floor(ms / DAY) }
  }

  if (ms >= HOUR) {
    return { unit: 'hour', value: Math.floor(ms / HOUR) }
  }

  if (ms >= MINUTE) {
    return { unit: 'minute', value: Math.floor(ms / MINUTE) }
  }

  return { unit: 'second', value: Math.floor(ms / SECOND) }
}

// Localized strings for `formatAgo`; shaped to accept `t.agents` directly.
export interface AgoLabels {
  ageNow: string
  ageSeconds: (seconds: number) => string
  ageMinutes: (minutes: number) => string
  ageHours: (hours: number) => string
  ageDays: (days: number) => string
}

// Compact localized "2h ago" / "3m ago" / "now" for a past timestamp, bucketed
// via `coarseElapsed` so every age label reads consistently.
export function formatAgo(fromMs: number, labels: AgoLabels, nowMs = Date.now()): string {
  const { unit, value } = coarseElapsed(nowMs - fromMs)

  if (unit === 'second') {
    return value < 2 ? labels.ageNow : labels.ageSeconds(value)
  }

  const by = { day: labels.ageDays, hour: labels.ageHours, minute: labels.ageMinutes }

  return by[unit](value)
}
