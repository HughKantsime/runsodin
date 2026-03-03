export default function EmptyState({ icon: Icon, title, description, children }) {
  return (
    <div className="text-center py-12">
      {Icon && <Icon size={32} className="mx-auto text-farm-600 mb-3" />}
      <p className="text-farm-500 text-base">{title}</p>
      {description && <p className="text-sm text-farm-600 mt-1">{description}</p>}
      {children && (
        <div className="mt-4 flex items-center justify-center gap-3">
          {children}
        </div>
      )}
    </div>
  )
}
