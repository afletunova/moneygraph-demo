import React, { useEffect, useState } from 'react'
import { C } from '../components/theme'
import { fmtUsd } from '../components/formatters'

function CandidateRow({ item, onRemove, onRestore, onCascade }) {
  const [approving, setApproving] = useState(false)
  // Pre-fill from known enrichment facts — type defaults to 'public'
  // only when facts.is_public is explicitly true (never guess dark_horse,
  // that's a manual promotion path); ticker comes from the facts.ticker the
  // backend attaches during enrichment for public candidates (SEC lookup).
  // Reviewer can still change either before submitting.
  const [form, setForm] = useState({
    name: item.name,
    type: item.facts?.is_public === true ? 'public' : 'private',
    ticker: item.facts?.ticker || '',
  })
  const [nameErr, setNameErr] = useState(false)
  const [linking, setLinking] = useState(false)
  const [linkQuery, setLinkQuery] = useState('')
  const [linkResults, setLinkResults] = useState([])
  const [linkErr, setLinkErr] = useState(null)

  useEffect(() => {
    if (!linking) return
    const q = linkQuery.trim()
    const url = q ? `/api/nodes?q=${encodeURIComponent(q)}` : '/api/nodes?limit=20'
    const id = setTimeout(() => {
      fetch(url).then(r => r.json()).then(setLinkResults).catch(() => setLinkResults([]))
    }, 150)
    return () => clearTimeout(id)
  }, [linking, linkQuery])

  function handleLink(node) {
    setLinkErr(null)
    fetch(`/api/candidates/${item.id}/link`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ node_id: node.id }),
    })
      .then(async r => {
        if (r.ok) { onRemove(item.id); return }
        const err = await r.json().catch(() => ({}))
        setLinkErr(r.status === 409
          ? `Already maps to ${err.owning_node_name || 'another node'}`
          : (err.error || 'Link failed'))
      })
      .catch(() => setLinkErr('Link failed'))
  }

  function handleApproveConfirm() {
    if (!form.name.trim()) { setNameErr(true); return }
    setNameErr(false)
    onRemove(item.id)
    fetch(`/api/candidates/${item.id}/approve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: form.name.trim(), type: form.type, ticker: form.ticker.trim() || null }),
    })
      .then(r => r.json())
      .then(data => { if (data.cascade_count > 0) onCascade(data.cascade_count) })
      .catch(() => onRestore(item))
  }

  function handleReject() {
    onRemove(item.id)
    fetch(`/api/candidates/${item.id}/reject`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    }).catch(() => onRestore(item))
  }

  const nodeNames = item.discovered_by_nodes_names || []
  const nodeCount = item.discovered_by_nodes_count || 0
  const facts = item.facts || null
  const factsLine = facts && [
    facts.short_description,
    [
      facts.sector,
      facts.founded,
      facts.headquarters,
      facts.is_public == null ? null : (facts.is_public ? 'public' : 'private'),
    ].filter(Boolean).join(' · '),
  ].filter(Boolean).join(' — ')

  return (
    <div style={{ borderBottom: `1px solid ${C.border}` }}>
      <div style={{
        padding: '12px 20px', display: 'flex', alignItems: 'flex-start',
        justifyContent: 'space-between', gap: 16,
      }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 3, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 13, fontWeight: 600, color: C.text }}>
              {item.name}
            </span>
            {item.discovery_count > 1 && (
              <span style={{
                fontSize: 10, fontWeight: 700, padding: '1px 5px',
                borderRadius: 3, background: `${C.warn}22`,
                border: `1px solid ${C.warn}66`, color: C.warn,
              }}>
                ×{item.discovery_count}
              </span>
            )}
          </div>
          {factsLine && (
            <div style={{ fontSize: 11, color: C.muted, marginBottom: 3 }}>
              {factsLine}
            </div>
          )}
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 11, color: C.muted }}>{item.discovered_via}</span>
            {nodeCount > 0 && (
              <span
                style={{ fontSize: 11, color: C.muted, cursor: nodeNames.length ? 'help' : 'default' }}
                title={nodeNames.length ? nodeNames.join(', ') : undefined}
              >
                discovered by {nodeCount} investor{nodeCount !== 1 ? 's' : ''}
              </span>
            )}
            {item.suggested_investor_name && nodeCount === 0 && (
              <span style={{ fontSize: 11, color: C.muted }}>
                via {item.suggested_investor_name}
              </span>
            )}
            {item.amount_usd != null && (
              <span style={{ fontSize: 11, color: C.success }}>{fmtUsd(item.amount_usd)}</span>
            )}
            <span style={{ fontSize: 11, color: C.muted }}>
              {item.discovered_at ? new Date(item.discovered_at).toLocaleDateString() : '—'}
            </span>
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8, flexShrink: 0 }}>
          <button
            onClick={() => { setApproving(a => !a); setLinking(false) }}
            style={{
              padding: '4px 12px', fontSize: 12, cursor: 'pointer', borderRadius: 4,
              background: approving ? `${C.success}22` : 'transparent',
              border: `1px solid ${approving ? C.success : C.border}`,
              color: approving ? C.success : C.text,
            }}
          >
            Approve
          </button>
          <button
            onClick={() => { setLinking(l => !l); setApproving(false); setLinkErr(null) }}
            style={{
              padding: '4px 12px', fontSize: 12, cursor: 'pointer', borderRadius: 4,
              background: linking ? `${C.public}22` : 'transparent',
              border: `1px solid ${linking ? C.public : C.border}`,
              color: linking ? C.public : C.text,
            }}
          >
            Link
          </button>
          <button
            onClick={handleReject}
            style={{
              padding: '4px 12px', fontSize: 12, cursor: 'pointer', borderRadius: 4,
              background: 'transparent', border: `1px solid ${C.border}`, color: C.danger,
            }}
          >
            Reject
          </button>
        </div>
      </div>
      {approving && (
        <div style={{
          padding: '10px 20px 14px', borderTop: `1px solid ${C.border}`,
          background: `${C.success}08`, display: 'flex', alignItems: 'flex-end', gap: 10, flexWrap: 'wrap',
        }}>
          <div>
            <div style={{ fontSize: 10, color: C.muted, marginBottom: 3 }}>Name</div>
            <input
              value={form.name}
              onChange={e => { setForm(f => ({ ...f, name: e.target.value })); setNameErr(false) }}
              style={{
                padding: '4px 8px', fontSize: 12, borderRadius: 4, outline: 'none',
                border: `1px solid ${nameErr ? C.danger : C.border}`,
                background: C.surface, color: C.text, width: 200,
              }}
            />
          </div>
          <div>
            <div style={{ fontSize: 10, color: C.muted, marginBottom: 3 }}>Type</div>
            <select
              value={form.type}
              onChange={e => setForm(f => ({ ...f, type: e.target.value }))}
              style={{
                padding: '4px 8px', fontSize: 12, borderRadius: 4, outline: 'none',
                border: `1px solid ${C.border}`, background: C.surface, color: C.text,
              }}
            >
              <option value="private">Private</option>
              <option value="public">Public</option>
              <option value="dark_horse">Dark Horse</option>
            </select>
          </div>
          <div>
            <div style={{ fontSize: 10, color: C.muted, marginBottom: 3 }}>Ticker (optional)</div>
            <input
              value={form.ticker}
              onChange={e => setForm(f => ({ ...f, ticker: e.target.value }))}
              placeholder="e.g. AAPL"
              style={{
                padding: '4px 8px', fontSize: 12, borderRadius: 4, outline: 'none',
                border: `1px solid ${C.border}`, background: C.surface, color: C.text, width: 100,
              }}
            />
          </div>
          <button
            onClick={handleApproveConfirm}
            style={{
              padding: '5px 14px', fontSize: 12, cursor: 'pointer', borderRadius: 4,
              background: C.success, border: 'none', color: '#0f1117', fontWeight: 600,
            }}
          >
            Confirm
          </button>
          <button
            onClick={() => setApproving(false)}
            style={{
              padding: '5px 10px', fontSize: 12, cursor: 'pointer', borderRadius: 4,
              background: 'transparent', border: `1px solid ${C.border}`, color: C.muted,
            }}
          >
            Cancel
          </button>
        </div>
      )}
      {linking && (
        <div style={{
          padding: '10px 20px 14px', borderTop: `1px solid ${C.border}`,
          background: `${C.public}08`,
        }}>
          <div style={{ fontSize: 10, color: C.muted, marginBottom: 5 }}>
            Link "{item.name}" to an existing node (registers it as an alias — no new node)
          </div>
          <input
            autoFocus
            value={linkQuery}
            onChange={e => setLinkQuery(e.target.value)}
            placeholder="Search nodes by name or ticker…"
            style={{
              padding: '5px 8px', fontSize: 12, borderRadius: 4, outline: 'none',
              border: `1px solid ${C.border}`, background: C.surface, color: C.text, width: 280,
            }}
          />
          {linkErr && (
            <div style={{ fontSize: 11, color: C.danger, marginTop: 5 }}>{linkErr}</div>
          )}
          <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 2, maxWidth: 400 }}>
            {linkResults.map(n => (
              <button
                key={n.id}
                onClick={() => handleLink(n)}
                style={{
                  textAlign: 'left', padding: '5px 8px', fontSize: 12, cursor: 'pointer',
                  borderRadius: 4, border: `1px solid ${C.border}`, background: C.surface, color: C.text,
                }}
              >
                {n.name}
                <span style={{ color: C.muted }}>
                  {n.ticker ? ` · ${n.ticker}` : ''} · {n.type}
                </span>
              </button>
            ))}
            {linkResults.length === 0 && (
              <div style={{ fontSize: 11, color: C.muted }}>No matching nodes.</div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

export default function ReviewQueueView() {
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [toast, setToast] = useState(null)

  function fetchItems() {
    fetch('/api/candidates')
      .then(r => r.json())
      .then(rows => { setItems(rows); setLoading(false) })
      .catch(() => setLoading(false))
  }

  useEffect(() => { fetchItems() }, [])

  function remove(id) {
    setItems(prev => prev.filter(i => i.id !== id))
  }

  function restore(item) {
    setItems(prev => {
      if (prev.find(i => i.id === item.id)) return prev
      return [item, ...prev]
    })
  }

  function handleCascade(count) {
    setToast(`Also collapsed ${count} duplicate${count !== 1 ? 's' : ''}.`)
    setTimeout(() => setToast(null), 4000)
    fetchItems()
  }

  if (loading) return <div style={{ padding: 40, color: C.muted, fontSize: 14 }}>Loading…</div>

  if (items.length === 0) {
    return <div style={{ padding: 40, color: C.muted, fontSize: 14 }}>No pending candidates.</div>
  }

  return (
    <div style={{ flex: 1, overflowY: 'auto' }}>
      {toast && (
        <div style={{
          padding: '8px 20px', fontSize: 12, color: C.success,
          background: `${C.success}11`, borderBottom: `1px solid ${C.success}33`,
        }}>
          {toast}
        </div>
      )}
      {items.map(item => (
        <CandidateRow
          key={item.id}
          item={item}
          onRemove={remove}
          onRestore={restore}
          onCascade={handleCascade}
        />
      ))}
    </div>
  )
}
