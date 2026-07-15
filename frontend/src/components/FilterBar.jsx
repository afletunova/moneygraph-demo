import React from 'react'
import { C } from './theme'

export default function FilterBar({ nodes, filters, onChange }) {
  const uniq = key => {
    const vals = new Set(nodes.map(n => n[key]).filter(v => v && String(v).trim()))
    return Array.from(vals).sort()
  }
  const sectors = uniq('sector')
  // Swapped from the sparse/imprecise `headquarters` city string to
  // the structured `country` field now that real country enrichment exists —
  // cleaner single "where" dimension, avoids two overlapping location filters.
  const countries = uniq('country')
  // Exchange dimension — same pattern, follow-up ask once exchange
  // became structured data via node_tickers instead of baked into the raw
  // ticker string.
  const exchanges = uniq('exchange')
  const active = filters.sector || filters.country || filters.type || filters.exchange

  const selStyle = {
    background: C.bg, color: C.text, border: `1px solid ${C.border}`,
    borderRadius: 4, padding: '4px 8px', fontSize: 12,
  }

  return (
    <div style={{
      position: 'absolute', top: 16, left: 16, zIndex: 10,
      background: C.surface, border: `1px solid ${C.border}`,
      borderRadius: 6, padding: '8px 12px', display: 'flex', gap: 10,
      alignItems: 'center',
    }}>
      <span style={{ fontSize: 11, color: C.muted }}>Highlight:</span>
      <select
        style={selStyle}
        value={filters.sector}
        onChange={e => onChange({ ...filters, sector: e.target.value })}
      >
        <option value="">Category — all</option>
        {sectors.map(s => <option key={s} value={s}>{s}</option>)}
      </select>
      <select
        style={selStyle}
        value={filters.country}
        onChange={e => onChange({ ...filters, country: e.target.value })}
      >
        <option value="">Country — all</option>
        {countries.map(c => <option key={c} value={c}>{c}</option>)}
      </select>
      <select
        style={selStyle}
        value={filters.type}
        onChange={e => onChange({ ...filters, type: e.target.value })}
      >
        <option value="">Role — all</option>
        <option value="public">Public</option>
        <option value="private">Private</option>
        <option value="dark_horse">Dark Horse</option>
      </select>
      <select
        style={selStyle}
        value={filters.exchange}
        onChange={e => onChange({ ...filters, exchange: e.target.value })}
      >
        <option value="">Exchange — all</option>
        {exchanges.map(x => <option key={x} value={x}>{x}</option>)}
      </select>
      {active && (
        <button
          onClick={() => onChange({ sector: '', country: '', type: '', exchange: '' })}
          style={{
            background: 'transparent', color: C.muted, border: `1px solid ${C.border}`,
            borderRadius: 4, padding: '4px 8px', fontSize: 11, cursor: 'pointer',
          }}
        >
          Clear
        </button>
      )}
    </div>
  )
}
