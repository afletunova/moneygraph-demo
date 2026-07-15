import React from 'react'
import { C } from './theme'

const TIER_COLOR = {
  1: '#22c55e',
  2: '#3b82f6',
  3: '#f59e0b',
  4: '#f97316',
  5: '#64748b',
}

export default function TierBadge({ tier }) {
  const color = TIER_COLOR[tier] ?? C.muted
  return (
    <span style={{
      fontSize: 10, fontWeight: 700, padding: '1px 6px',
      borderRadius: 3, border: `1px solid ${color}`,
      color, lineHeight: '16px', whiteSpace: 'nowrap', flexShrink: 0,
    }}>
      T{tier}
    </span>
  )
}
