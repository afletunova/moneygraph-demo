import React from 'react'
import { C } from './theme'

export default function TabBar({ active, onChange }) {
  const tabs = ['Graph', 'Nodes', 'News', 'Review Queue', 'Runs', 'Settings']
  return (
    <div style={{
      display: 'flex', background: C.surface, borderBottom: `1px solid ${C.border}`,
      flexShrink: 0,
    }}>
      {tabs.map(t => (
        <button key={t} onClick={() => onChange(t)} style={{
          padding: '9px 18px', border: 'none', background: 'none', cursor: 'pointer',
          color: active === t ? C.text : C.muted, fontSize: 13, fontWeight: active === t ? 600 : 400,
          borderBottom: active === t ? `2px solid ${C.public}` : '2px solid transparent',
          transition: 'color 0.15s',
        }}>
          {t}
        </button>
      ))}
    </div>
  )
}

