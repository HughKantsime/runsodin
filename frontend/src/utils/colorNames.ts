// Common filament color names with hex values
const COLOR_NAMES: Record<string, string> = {
  '000000': 'Black',
  'FFFFFF': 'White',
  'F5F5F5': 'Jade White',
  'FF0000': 'Red',
  '00FF00': 'Green',
  '0000FF': 'Blue',
  'FFFF00': 'Yellow',
  'FFA500': 'Orange',
  '800080': 'Purple',
  'FFC0CB': 'Pink',
  'A52A2A': 'Brown',
  '808080': 'Gray',
  'C0C0C0': 'Silver',
  'FFD700': 'Gold',
  '00FFFF': 'Cyan',
  'FF00FF': 'Magenta',
  '000080': 'Navy',
  '008000': 'Dark Green',
  '8B4513': 'Saddle Brown',
  'D2691E': 'Chocolate',
  'F0E68C': 'Khaki',
  '98FB98': 'Pale Green',
  'ADD8E6': 'Light Blue',
  'DDA0DD': 'Plum',
  'F5DEB3': 'Wheat',
  'D3D3D3': 'Light Gray',
  'A9A9A9': 'Dark Gray',
  'ADB1B2': 'Gray',
  '424379': 'Galaxy Purple',
  '0078BF': 'Blue',
  'D1D3D5': 'Silver',
}

function hexToRgb(hex: string): { r: number; g: number; b: number } {
  const h = hex.replace('#', '')
  return {
    r: parseInt(h.substring(0, 2), 16),
    g: parseInt(h.substring(2, 4), 16),
    b: parseInt(h.substring(4, 6), 16),
  }
}

function colorDistance(hex1: string, hex2: string): number {
  const c1 = hexToRgb(hex1)
  const c2 = hexToRgb(hex2)
  return Math.sqrt(
    Math.pow(c1.r - c2.r, 2) +
    Math.pow(c1.g - c2.g, 2) +
    Math.pow(c1.b - c2.b, 2)
  )
}

export function getColorName(hex: string | null | undefined): string | null {
  if (!hex) return null
  const h = hex.replace('#', '').toUpperCase()

  if (COLOR_NAMES[h]) return COLOR_NAMES[h]

  let closest: string | null = null
  let minDist = Infinity

  for (const [colorHex, name] of Object.entries(COLOR_NAMES)) {
    const dist = colorDistance(h, colorHex)
    if (dist < minDist) {
      minDist = dist
      closest = name
    }
  }

  return closest
}
