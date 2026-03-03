import { Search, X } from 'lucide-react'
import clsx from 'clsx'

export default function SearchInput({
  value,
  onChange,
  placeholder = 'Search...',
  className,
  ...rest
}) {
  return (
    <div className={clsx('relative', className)}>
      <Search
        size={16}
        className="absolute left-3 top-1/2 -translate-y-1/2 text-farm-500"
      />
      <input
        type="text"
        value={value}
        onChange={onChange}
        placeholder={placeholder}
        className="w-full bg-farm-800 border border-farm-700 rounded-lg pl-9 pr-3 py-2 text-sm"
        {...rest}
      />
      {value && (
        <button
          type="button"
          onClick={() =>
            onChange({ target: { value: '' } })
          }
          className="absolute right-2 top-1/2 -translate-y-1/2 p-0.5 text-farm-500 hover:text-farm-300 transition-colors"
          aria-label="Clear search"
        >
          <X size={14} />
        </button>
      )}
    </div>
  )
}
