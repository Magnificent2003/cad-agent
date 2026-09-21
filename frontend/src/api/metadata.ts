import type { Metadata } from './types'

export function metadataInfo(value: Metadata | null | undefined = {}) {
  const metadata = value || {}
  const units = metadata.units as { code?: number; name?: string; confirmed?: boolean } | undefined
  const unitNames: Record<string, string> = {
    Millimeters: '毫米',
    millimeters: '毫米',
    mm: '毫米',
    Meters: '米',
    meters: '米',
    m: '米',
    Centimeters: '厘米',
    centimeters: '厘米',
    cm: '厘米',
    Inches: '英寸',
    inches: '英寸',
    Feet: '英尺',
    feet: '英尺',
    unitless: '待确认',
    Unitless: '待确认',
    unspecified: '待确认',
  }
  const unit =
    !units || units.code === 0 || units.confirmed === false
      ? '待确认'
      : unitNames[units.name || ''] || units.name || '待确认'
  const layouts = Array.isArray(metadata.layouts)
    ? metadata.layouts
        .map((item: unknown) =>
          typeof item === 'string' ? item : String((item as { name: string }).name),
        )
        .filter(Boolean)
    : ['Model']
  const counts = (metadata.entity_counts || {}) as Record<string, number>
  const entityCount = Object.values(counts).reduce((total, n) => total + (Number(n) || 0), 0)
  const layerCount = Array.isArray(metadata.layers)
    ? metadata.layers.length
    : Number(metadata.layer_count || 0)
  const warnings = Array.isArray(metadata.warnings)
    ? metadata.warnings.filter((w): w is string => typeof w === 'string')
    : []
  return {
    unit,
    layouts: layouts.length ? layouts : ['Model'],
    counts,
    entityCount,
    layerCount,
    warnings,
  }
}

export function layoutLabel(layout: string) {
  return layout === 'Model' ? '模型空间' : layout
}
