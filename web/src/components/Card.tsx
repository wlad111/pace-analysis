import type { ReactNode } from 'react'

export interface CardProps {
  title: string
  caption?: string
  /** Controls in the card header: table-view toggles, scale switches, selects. */
  actions?: ReactNode
  /** Dim the body while the next slice loads, instead of a skeleton flash. */
  stale?: boolean
  children: ReactNode
}

/** Chart / table container: title, caption, header controls, body. */
export function Card({ title, caption, actions, stale = false, children }: CardProps) {
  return (
    <figure className="card">
      <div className="card-head">
        <figcaption>
          <h2>{title}</h2>
          {caption !== undefined && <div className="caption">{caption}</div>}
        </figcaption>
        {actions !== undefined && <div className="card-actions">{actions}</div>}
      </div>
      <div className={stale ? 'card-body is-stale' : 'card-body'}>{children}</div>
    </figure>
  )
}

export interface ViewToggleProps<T extends string> {
  value: T
  options: readonly { value: T; label: string }[]
  onChange: (value: T) => void
  label: string
}

/** Segmented control used for chart/table view and for the outlier scale mode. */
export function ViewToggle<T extends string>({
  value,
  options,
  onChange,
  label,
}: ViewToggleProps<T>) {
  return (
    <div className="segmented" role="group" aria-label={label}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={option.value === value}
          onClick={() => {
            onChange(option.value)
          }}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}
