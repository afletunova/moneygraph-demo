import React from 'react'
import { C } from './theme'

export default function Legend() {
  const items = [
    { label: 'Public', color: C.public },
    { label: 'Private', color: C.private },
    { label: 'Dark Horse', color: C.dark_horse },
  ]
  return (
    <div style={{
      position: 'absolute', bottom: 16, left: 16,
      background: C.surface, border: `1px solid ${C.border}`,
      borderRadius: 6, padding: '8px 12px', display: 'flex', gap: 12,
      pointerEvents: 'none', zIndex: 10,
    }}>
      {items.map(({ label, color }) => (
        <div key={label} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <div style={{ width: 10, height: 10, borderRadius: '50%', background: color }} />
          <span style={{ fontSize: 11, color: C.muted }}>{label}</span>
        </div>
      ))}
    </div>
  )
}
