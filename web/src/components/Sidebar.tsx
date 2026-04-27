import { NavLink } from 'react-router-dom'
import { Activity, Sparkles } from 'lucide-react'
import { cn } from '@/lib/utils'

const items = [
  { to: '/', label: 'Today', icon: Sparkles, end: true },
  { to: '/trends', label: 'Trends', icon: Activity, end: false },
]

function BrandMark({ size = 'md' }: { size?: 'sm' | 'md' }) {
  const tile = size === 'sm' ? 'size-6' : 'size-7'
  const icon = size === 'sm' ? 'size-3.5' : 'size-4'
  return (
    <span className="inline-flex items-center gap-2">
      <span className={cn(tile, 'rounded-md bg-accent flex items-center justify-center')}>
        <svg viewBox="0 0 32 32" className={cn(icon, 'text-bg')} fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
          <path d="M8 18l4-8 4 6 4-4 4 6" />
        </svg>
      </span>
      <span className="font-semibold tracking-tight">fitness</span>
    </span>
  )
}

/** Desktop sidebar — full nav with brand + footer. Hidden on mobile. */
export function Sidebar() {
  return (
    <aside className="hidden md:flex w-56 shrink-0 border-r border-border bg-bg/60 backdrop-blur flex-col">
      <div className="px-5 py-5">
        <BrandMark />
      </div>

      <nav className="flex-1 px-2 py-2 space-y-0.5">
        {items.map((item) => {
          const Icon = item.icon
          return (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                cn(
                  'flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition-colors',
                  isActive
                    ? 'bg-surface text-text shadow-card'
                    : 'text-muted hover:text-text hover:bg-surface/60',
                )
              }
            >
              <Icon className="size-4" />
              {item.label}
            </NavLink>
          )
        })}
      </nav>

      <div className="px-5 py-4 text-[11px] text-faint">
        local · Garmin → Claude
      </div>
    </aside>
  )
}

/**
 * Mobile-only top bar — compact brand on the left, tab nav on the right.
 * Sticky so the nav remains reachable when the user scrolls. Hidden on
 * md+ where the desktop sidebar takes over.
 */
export function MobileTopBar() {
  return (
    <header className="md:hidden sticky top-0 z-20 bg-bg/85 backdrop-blur border-b border-border">
      <div className="flex items-center justify-between px-4 py-2.5">
        <BrandMark size="sm" />
        <nav className="flex items-center gap-1">
          {items.map((item) => {
            const Icon = item.icon
            return (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  cn(
                    'inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-[13px] transition-colors',
                    isActive
                      ? 'bg-surface text-text border border-border'
                      : 'text-muted hover:text-text',
                  )
                }
              >
                <Icon className="size-3.5" />
                {item.label}
              </NavLink>
            )
          })}
        </nav>
      </div>
    </header>
  )
}
