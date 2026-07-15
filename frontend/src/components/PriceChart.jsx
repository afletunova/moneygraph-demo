import React from 'react'
import { C } from './theme'
import { fmtPrice } from './formatters'

// Small inline SVG line chart — no charting library dependency (frontend
// only has d3 + react). Draws close-price points scaled to the viewBox;
// green if the range ended up, red if it ended down, flat muted if only one
// point (nothing to compare).
export default function PriceChart({ points }) {
  const width = 280
  const height = 90
  const pad = 4

  if (!points || points.length < 2) {
    return (
      <div style={{
        height, display: 'flex', alignItems: 'center', justifyContent: 'center',
        color: C.muted, fontSize: 11,
      }}>
        {points && points.length === 1 ? 'Not enough data to chart.' : 'No price data.'}
      </div>
    )
  }

  const closes = points.map(p => p.close)
  const min = Math.min(...closes)
  const max = Math.max(...closes)
  const span = max - min || 1
  const up = closes[closes.length - 1] >= closes[0]
  const color = up ? C.success : C.danger

  const xy = closes.map((c, i) => {
    const x = pad + (i / (closes.length - 1)) * (width - 2 * pad)
    const y = pad + (1 - (c - min) / span) * (height - 2 * pad)
    return `${x.toFixed(1)},${y.toFixed(1)}`
  })

  return (
    <div>
      <svg width="100%" viewBox={`0 0 ${width} ${height}`} style={{ display: 'block' }}>
        <polyline points={xy.join(' ')} fill="none" stroke={color} strokeWidth="1.5" />
      </svg>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10, color: C.muted, marginTop: 2 }}>
        <span>{fmtPrice(min)}</span>
        <span style={{ color }}>{fmtPrice(closes[closes.length - 1])}</span>
        <span>{fmtPrice(max)}</span>
      </div>
    </div>
  )
}

