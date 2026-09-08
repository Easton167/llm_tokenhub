type BrandProps = {
  subtitle?: string
  compact?: boolean
}

export function Brand({ subtitle, compact }: BrandProps) {
  return (
    <div className={`th-brand${compact ? ' th-brand-compact' : ''}`}>
      <span className="th-brand-mark" aria-hidden>
        T
      </span>
      {!compact ? (
        <div className="th-brand-text">
          <div className="th-brand-name">TokenHub</div>
          {subtitle ? <div className="th-brand-sub">{subtitle}</div> : null}
        </div>
      ) : null}
    </div>
  )
}
