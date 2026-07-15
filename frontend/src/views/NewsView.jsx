import React, { useEffect, useState } from 'react'
import { C } from '../components/theme'
import { fmtUsd } from '../components/formatters'
import TierBadge from '../components/TierBadge'

function useNewsFeed() {
  const [items, setItems] = useState([])
  const [offset, setOffset] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(true)

  function load(o) {
    setLoading(true)
    fetch(`/api/news?limit=50&offset=${o}`)
      .then(r => r.json())
      .then(rows => {
        setItems(prev => o === 0 ? rows : [...prev, ...rows])
        setHasMore(rows.length === 50)
        setOffset(o + rows.length)
        setLoading(false)
      })
      .catch(() => setLoading(false))
  }

  useEffect(() => { load(0) }, [])

  return { items, hasMore, loading, loadMore: () => load(offset) }
}

// TODO(Step 17 follow-up): NewsFeed rows don't render entity facts (short_description
// / sector) yet — /news's canonical_investor/canonical_investee are names, not node
// ids, so there's no key to join node_facts on without restructuring the endpoint.
export default function NewsView() {
  const { items, hasMore, loading, loadMore } = useNewsFeed()

  if (loading && items.length === 0) {
    return <div style={{ padding: 40, color: C.muted, fontSize: 14 }}>Loading…</div>
  }

  if (items.length === 0) {
    return (
      <div style={{ padding: 40, color: C.muted, fontSize: 14 }}>
        No news yet — run the pipeline with a wider lookback window.
      </div>
    )
  }

  return (
    <div style={{ flex: 1, overflowY: 'auto' }}>
      {items.map(item => (
        <div key={item.id} style={{
          padding: '12px 20px',
          borderBottom: `1px solid ${C.border}`,
          background: item.confirmed_by_sec ? `${C.success}0d` : 'transparent',
        }}>
          <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, marginBottom: 4 }}>
            <TierBadge tier={item.source_tier} />
            <a
              href={item.url}
              target="_blank"
              rel="noreferrer"
              style={{ color: C.text, fontSize: 13, fontWeight: 500, lineHeight: 1.4, textDecoration: 'none' }}
            >
              {item.headline}
            </a>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', paddingLeft: 34 }}>
            <span style={{ fontSize: 11, color: C.muted }}>{item.source_name}</span>
            <span style={{ fontSize: 11, color: C.muted }}>
              {item.published_at ? new Date(item.published_at).toLocaleDateString() : '—'}
            </span>
            {(item.canonical_investor || item.canonical_investee) && (
              <span style={{ fontSize: 11, color: C.muted }}>
                {item.canonical_investor || item.extracted_investor}
                {' → '}
                {item.canonical_investee || item.extracted_investee}
                {item.canonical_investor && item.canonical_investor !== item.extracted_investor && (
                  <sup style={{ fontSize: 9, color: C.muted, marginLeft: 2 }}
                       title={`was "${item.extracted_investor}"`}>~</sup>
                )}
                {item.canonical_investee && item.canonical_investee !== item.extracted_investee && (
                  <sup style={{ fontSize: 9, color: C.muted, marginLeft: 2 }}
                       title={`was "${item.extracted_investee}"`}>~</sup>
                )}
              </span>
            )}
            {item.amount_usd != null && (
              <span style={{ fontSize: 11, color: C.success }}>{fmtUsd(item.amount_usd)}</span>
            )}
            {item.confirmed_by_sec && (
              <span style={{
                fontSize: 10, fontWeight: 700, padding: '1px 5px',
                borderRadius: 3, background: `${C.success}22`,
                border: `1px solid ${C.success}66`, color: C.success,
              }}>
                SEC
              </span>
            )}
          </div>
        </div>
      ))}
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
  )
}
