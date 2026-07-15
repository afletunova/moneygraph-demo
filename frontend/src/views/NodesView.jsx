import React, { useEffect, useState } from 'react'
import { C, NODE_COLOR } from '../components/theme'
import NodePanel from '../components/NodePanel'

// Full browsable node list (companion to the force graph — jump to
// a node by name/ticker without clicking through the graph, matters more at
// scale). Reuses NodePanel (the node detail side panel) for row-click
// detail/edit rather than building a second detail view — mirrors how
// GraphView opens it (selectedNode state + onClose/onUpdated).
const NODE_LIST_COLUMNS = [
  { key: 'name', label: 'Name', sortable: true },
  { key: 'ticker', label: 'Ticker', sortable: false },
  { key: 'type', label: 'Type', sortable: true },
  { key: 'sector', label: 'Sector', sortable: true },
  { key: 'country', label: 'Country', sortable: true },
  { key: 'edge_count', label: 'Edges', sortable: true },
]

function useNodesList() {
  const [items, setItems] = useState([])
  const [offset, setOffset] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(true)
  const [q, setQ] = useState('')
  const [sort, setSort] = useState('name')
  const [order, setOrder] = useState('asc')

  function load(o, query, s, ord) {
    setLoading(true)
    const params = new URLSearchParams({ limit: '50', offset: String(o), sort: s, order: ord })
    if (query) params.set('q', query)
    fetch(`/api/nodes?${params}`)
      .then(r => r.json())
      .then(rows => {
        setItems(prev => o === 0 ? rows : [...prev, ...rows])
        setHasMore(rows.length === 50)
        setOffset(o + rows.length)
        setLoading(false)
      })
      .catch(() => setLoading(false))
  }

  // Debounced like CandidateRow's link-typeahead (150ms) — also re-triggers
  // on sort/order changes (always resets to offset 0), a slightly-delayed
  // sort click is an acceptable tradeoff for one shared effect.
  useEffect(() => {
    const id = setTimeout(() => load(0, q, sort, order), 150)
    return () => clearTimeout(id)
  }, [q, sort, order])

  function toggleSort(col) {
    if (sort === col) {
      setOrder(o => (o === 'asc' ? 'desc' : 'asc'))
    } else {
      setSort(col)
      setOrder('asc')
    }
  }

  return { items, hasMore, loading, q, setQ, sort, order, toggleSort, loadMore: () => load(offset, q, sort, order) }
}

export default function NodesView({ onGraphRefresh }) {
  const { items, hasMore, loading, q, setQ, sort, order, toggleSort, loadMore } = useNodesList()
  const [selectedNode, setSelectedNode] = useState(null)

  const thStyle = sortable => ({
    textAlign: 'left', padding: '8px 12px', fontSize: 11, color: C.muted,
    cursor: sortable ? 'pointer' : 'default', userSelect: 'none',
    borderBottom: `1px solid ${C.border}`, whiteSpace: 'nowrap',
  })

  return (
    <div style={{ flex: 1, position: 'relative', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      <div style={{ padding: '12px 20px', borderBottom: `1px solid ${C.border}`, flexShrink: 0 }}>
        <input
          value={q}
          onChange={e => setQ(e.target.value)}
          placeholder="Search nodes by name or ticker…"
          style={{
            padding: '6px 10px', fontSize: 13, borderRadius: 4, outline: 'none',
            border: `1px solid ${C.border}`, background: C.surface, color: C.text, width: 320,
          }}
        />
      </div>
      <div style={{ flex: 1, overflowY: 'auto' }}>
        {items.length === 0 && !loading ? (
          <div style={{ padding: 40, color: C.muted, fontSize: 14 }}>No nodes found.</div>
        ) : (
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr>
                {NODE_LIST_COLUMNS.map(col => (
                  <th
                    key={col.key}
                    onClick={() => col.sortable && toggleSort(col.key)}
                    style={thStyle(col.sortable)}
                  >
                    {col.label}
                    {sort === col.key && (order === 'asc' ? ' ▲' : ' ▼')}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {items.map(n => (
                <tr
                  key={n.id}
                  onClick={() => setSelectedNode(n)}
                  style={{ cursor: 'pointer', borderBottom: `1px solid ${C.border}` }}
                >
                  <td style={{ padding: '8px 12px', fontSize: 13, color: C.text }}>
                    {n.name}
                    {n.acquisition_flagged && (
                      <span
                        title="Possible acquisition — flagged as a candidate for private reclassification"
                        style={{ marginLeft: 6, fontSize: 11, color: C.warn }}
                      >
                        ⚠
                      </span>
                    )}
                  </td>
                  <td style={{ padding: '8px 12px', fontSize: 12, color: C.muted }}>{n.ticker || '—'}</td>
                  <td style={{ padding: '8px 12px', fontSize: 12 }}>
                    <span style={{ textTransform: 'capitalize', color: NODE_COLOR[n.type] ?? C.muted }}>
                      {n.type?.replace('_', ' ')}
                    </span>
                  </td>
                  <td style={{ padding: '8px 12px', fontSize: 12, color: C.muted }}>{n.sector || '—'}</td>
                  <td style={{ padding: '8px 12px', fontSize: 12, color: C.muted }}>{n.country || '—'}</td>
                  <td style={{ padding: '8px 12px', fontSize: 12, color: C.muted }}>{n.edge_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {hasMore && (
          <div style={{ padding: '16px 20px', display: 'flex', justifyContent: 'center' }}>
            <button
              onClick={loadMore}
              disabled={loading}
              style={{
                padding: '7px 20px', fontSize: 13,
                cursor: loading ? 'default' : 'pointer',
                background: C.surface, border: `1px solid ${C.border}`,
                color: loading ? C.muted : C.text, borderRadius: 5,
              }}
            >
              {loading ? 'Loading…' : 'Load more'}
            </button>
          </div>
        )}
      </div>
      {selectedNode && (
        <NodePanel
          node={selectedNode}
          onClose={() => setSelectedNode(null)}
          onUpdated={onGraphRefresh}
        />
      )}
    </div>
  )
}

