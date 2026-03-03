export default function PageHeader({ icon: Icon, title, subtitle, children }) {
  return (
    <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-4 md:mb-6">
      <div className="flex items-center gap-3">
        {Icon && <Icon className="text-print-400" size={24} />}
        <div>
          <h1
            className="text-xl md:text-2xl font-display font-bold"
            style={{ color: 'var(--brand-text-primary)' }}
          >
            {title}
          </h1>
          {subtitle && <p className="text-farm-500 text-sm mt-1">{subtitle}</p>}
        </div>
      </div>
      {children && (
        <div className="flex items-center gap-2 self-start">{children}</div>
      )}
    </div>
  )
}
