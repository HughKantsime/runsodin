const COLOR_MAP = {
  black: '#000000',
  white: '#FFFFFF',
  red: '#EF4444',
  blue: '#3B82F6',
  green: '#22C55E',
  yellow: '#EAB308',
  orange: '#F97316',
  purple: '#A855F7',
  pink: '#EC4899',
  gray: '#6B7280',
  grey: '#6B7280',
  brown: '#92400E',
  'caramel matte': '#C68E5B',
  'green matte': '#4A7C59',
  'lime green matte': '#84CC16',
  'maroon red': '#7F1D1D',
  'forest green': '#166534',
  'navy blue': '#1E3A5F',
  'sky blue': '#38BDF8',
  'dark grey': '#374151',
  'light grey': '#D1D5DB',
  'bambu lab pla': '#FFFFFF',
  'bambu lab pla basic': '#FFFFFF',
  'bambu lab pla matte': '#E5E5E5',
  'bambu lab pla galaxy': '#2D2B55',
  'bambu lab petg hf': '#F5F5F0',
  'bambu lab petg translucent': '#E0E0E0',
  'bambu lab support for pla': '#CCCCCC',
}

export function resolveColor(name) {
  if (!name) return null
  const trimmed = name.trim()
  if (trimmed.startsWith('#')) return trimmed
  if (trimmed.startsWith('rgb') || trimmed.startsWith('hsl')) return trimmed
  const lower = trimmed.toLowerCase()
  if (COLOR_MAP[lower]) return COLOR_MAP[lower]
  const simplified = lower.replace(/\s*(matte|glossy|silk|metallic)\s*/g, '').trim()
  if (COLOR_MAP[simplified]) return COLOR_MAP[simplified]
  const basicCSS = ['black','white','red','blue','green','yellow','orange','purple','pink','gray','grey','brown','cyan','magenta','lime','navy','teal','maroon','olive','silver','gold']
  const cssTest = lower.replace(/\s+/g, '')
  if (basicCSS.includes(cssTest)) return cssTest
  return null
}
