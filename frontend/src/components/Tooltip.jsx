import React from 'react'
import { C } from './theme'

export default function Tooltip({ tip }) {
  if (!tip) return null
  return (
    <div style={{
      position: 'fixed', left: tip.x + 14, top: tip.y + 14,
      background: C.surface, border: `1px solid ${C.border}`,
      borderRadius: 6, padding: '8px 12px', fontSize: 12, color: C.text,
      pointerEvents: 'none', zIndex: 200, maxWidth: 280,
      boxShadow: '0 4px 16px rgba(0,0,0,0.6)',
    }}>
      {tip.content}
    </div>
  )
}

