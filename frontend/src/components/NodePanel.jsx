import React, { useEffect, useState } from 'react'
import { C, NODE_COLOR } from './theme'
import { fmtSplitAmount, fmtUsd } from './formatters'
import PriceChart from './PriceChart'
import TierBadge from './TierBadge'

const PRICE_RANGES = ['1d', '1m', '1y', '5y', 'max']

export default function NodePanel({ node, onClose, onUpdated }) {
  const [detail, setDetail] = useState(null)
  const [range, setRange] = useState('1y')
  const [price, setPrice] = useState(null)
  const [priceLoading, setPriceLoading] = useState(false)
  const [news, setNews] = useState(null)
  const [editing, setEditing] = useState(false)
  const [form, setForm] = useState({ type: '', cik: '', sector: '' })
  const [saving, setSaving] = useState(false)
  const [saveErr, setSaveErr] = useState(null)
  // Node's full ticker/exchange list (node_tickers), separate from
  // `form` above — each ticker add/remove/promote is its own immediate
  // request (not batched into the type/cik/sector Save button) since it has
  // its own endpoint (POST/DELETE /nodes/{id}/tickers) and its own primary-
  // sync side effect on nodes.ticker.
  const [tickers, setTickers] = useState([])
  const [newTicker, setNewTicker] = useState('')
  const [tickerErr, setTickerErr] = useState(null)
  const [tickerBusy, setTickerBusy] = useState(false)
  // Surfaces nodes.meta.acquisition_demotion_candidate
  // (written by enrichment.flag_acquisition_demotion_candidate, never
  // auto-applied). "Confirm
  // demotion" just calls the same evidence-gated POST /update the type
  // dropdown's Save button already uses; no new validation here.
  const [demoteBusy, setDemoteBusy] = useState(false)
  const [demoteErr, setDemoteErr] = useState(null)

  function loadDetail() {
    if (!node) return
    fetch(`/api/nodes/${node.id}`)
      .then(r => r.json())
      .then(d => {
        setDetail(d)
        setForm({
          type: d.type || 'private',
          cik: d.cik || '',
          sector: d.sector || '',
        })
      })
      .catch(() => {})
  }

  function loadTickers() {
    if (!node) return
    fetch(`/api/nodes/${node.id}/tickers`)
      .then(r => r.json())
      .then(rows => setTickers(Array.isArray(rows) ? rows : []))
      .catch(() => setTickers([]))
  }

  useEffect(() => {
    setDetail(null)
    setNews(null)
    setEditing(false)
    setSaveErr(null)
    setTickerErr(null)
    setNewTicker('')
    loadDetail()
    loadTickers()
    fetch(`/api/news?node_id=${node.id}&limit=20`)
      .then(r => r.json())
      .then(setNews)
      .catch(() => setNews([]))
  }, [node?.id])

  // Add the ticker in `newTicker` (bare "AAPL" or exchange-qualified
  // "HKG: 9988") as an additional ticker on this node; the first ticker ever
  // added is forced primary by the backend regardless of this call's intent.
  function handleAddTicker() {
    const raw = newTicker.trim()
    if (!raw) return
    setTickerBusy(true)
    setTickerErr(null)
    fetch(`/api/nodes/${node.id}/tickers`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticker: raw }),
    })
      .then(async r => {
        setTickerBusy(false)
        if (!r.ok) {
          const err = await r.json().catch(() => ({}))
          setTickerErr(err.error || 'Add failed')
          return
        }
        setNewTicker('')
        loadTickers()
        loadDetail()
        if (onUpdated) onUpdated()
      })
      .catch(() => { setTickerBusy(false); setTickerErr('Add failed') })
  }

  function handleSetPrimaryTicker(t) {
    setTickerBusy(true)
    setTickerErr(null)
    const raw = t.exchange ? `${t.exchange}: ${t.ticker}` : t.ticker
    fetch(`/api/nodes/${node.id}/tickers`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticker: raw, is_primary: true }),
    })
      .then(async r => {
        setTickerBusy(false)
        if (!r.ok) {
          const err = await r.json().catch(() => ({}))
          setTickerErr(err.error || 'Update failed')
          return
        }
        loadTickers()
        loadDetail()
        if (onUpdated) onUpdated()
      })
      .catch(() => { setTickerBusy(false); setTickerErr('Update failed') })
  }

  function handleRemoveTicker(t) {
    setTickerBusy(true)
    setTickerErr(null)
    fetch(`/api/nodes/${node.id}/tickers/${t.id}`, { method: 'DELETE' })
      .then(async r => {
        setTickerBusy(false)
        if (!r.ok) {
          const err = await r.json().catch(() => ({}))
          setTickerErr(err.error || 'Remove failed')
          return
        }
        loadTickers()
        loadDetail()
        if (onUpdated) onUpdated()
      })
      .catch(() => { setTickerBusy(false); setTickerErr('Remove failed') })
  }

  useEffect(() => {
    if (!detail?.ticker) { setPrice(null); return }
    setPriceLoading(true)
    fetch(`/api/nodes/${node.id}/price?range=${range}`)
      .then(r => r.json())
      .then(d => { setPrice(d); setPriceLoading(false) })
      .catch(() => { setPrice(null); setPriceLoading(false) })
  }, [node?.id, detail?.ticker, range])

  if (!node) return null

  function handleSave() {
    setSaving(true)
    setSaveErr(null)
    fetch(`/api/nodes/${node.id}/update`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        type: form.type,
        cik: form.cik.trim() || null,
        sector: form.sector.trim() || null,
      }),
    })
      .then(async r => {
        setSaving(false)
        if (!r.ok) {
          const err = await r.json().catch(() => ({}))
          setSaveErr(err.error || 'Save failed')
          return
        }
        const updated = await r.json()
        setDetail(updated)
        setEditing(false)
        if (onUpdated) onUpdated()
      })
      .catch(() => { setSaving(false); setSaveErr('Save failed') })
  }

  function handleConfirmDemotion() {
    setDemoteBusy(true)
    setDemoteErr(null)
    fetch(`/api/nodes/${node.id}/update`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type: 'private' }),
    })
      .then(async r => {
        setDemoteBusy(false)
        if (!r.ok) {
          const err = await r.json().catch(() => ({}))
          setDemoteErr(err.error || 'Confirm failed')
          return
        }
        const updated = await r.json()
        setDetail(updated)
        setForm(f => ({ ...f, type: updated.type || f.type }))
        loadTickers()
        if (onUpdated) onUpdated()
      })
      .catch(() => { setDemoteBusy(false); setDemoteErr('Confirm failed') })
  }

  const d = detail
  const demotionCandidate = d?.meta?.acquisition_demotion_candidate
  const typeColor = NODE_COLOR[d?.type] ?? C.muted
  // Mirror the backend's node.type transition rules (main.py
  // _ALLOWED_TYPE_TRANSITIONS) so the select only offers moves that will
  // actually be accepted: dark_horse -> anything, private -> public/private,
  // public -> public only (no demotion).
  const typeOptions = d?.type === 'public'
    ? ['public']
    : d?.type === 'private'
      ? ['private', 'public']
      : ['dark_horse', 'private', 'public']

  return (
    <div style={{
      position: 'absolute', right: 0, top: 0, bottom: 0, width: 320,
      background: C.surface, borderLeft: `1px solid ${C.border}`,
      display: 'flex', flexDirection: 'column', zIndex: 50,
    }}>
      <div style={{
        display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between',
        padding: '12px 16px', borderBottom: `1px solid ${C.border}`, gap: 8,
      }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 14, fontWeight: 600, color: C.text, marginBottom: 2 }}>
            {node.name}
          </div>
          <div style={{ fontSize: 11, textTransform: 'capitalize', color: typeColor }}>
            {d?.type?.replace('_', ' ') || '…'}
            {d?.ticker && <span style={{ color: C.muted }}> · {d.ticker}</span>}
          </div>
        </div>
        <button onClick={onClose} style={{
          background: 'none', border: 'none', color: C.muted, cursor: 'pointer',
          fontSize: 18, lineHeight: 1, padding: '2px 4px', flexShrink: 0,
        }}>
          ×
        </button>
      </div>

      <div style={{ flex: 1, overflowY: 'auto' }}>
        {!d ? (
          <div style={{ padding: 16, color: C.muted, fontSize: 12 }}>Loading…</div>
        ) : (
          <>
            {demotionCandidate && (
              <div style={{
                margin: '10px 16px 0', padding: '8px 10px', borderRadius: 6,
                background: `${C.warn}18`, border: `1px solid ${C.warn}`,
              }}>
                <div style={{ fontSize: 12, color: C.warn, fontWeight: 600, marginBottom: 4 }}>
                  ⚠ Possible acquisition
                </div>
                <div style={{ fontSize: 11, color: C.text, marginBottom: 6 }}>
                  Flagged as a candidate for private reclassification — acquired by{' '}
                  {demotionCandidate.acquirer_name || 'an unknown acquirer'}.
                </div>
                <button
                  onClick={handleConfirmDemotion}
                  disabled={demoteBusy}
                  style={{
                    padding: '4px 10px', fontSize: 11, borderRadius: 4,
                    cursor: demoteBusy ? 'default' : 'pointer',
                    background: C.warn, border: 'none', color: '#0f1117', fontWeight: 600,
                    opacity: demoteBusy ? 0.6 : 1,
                  }}
                >
                  {demoteBusy ? 'Confirming…' : 'Confirm demotion'}
                </button>
                {demoteErr && <div style={{ fontSize: 11, color: C.danger, marginTop: 4 }}>{demoteErr}</div>}
              </div>
            )}
            {/* Facts */}
            <div style={{ padding: '10px 16px', borderBottom: `1px solid ${C.border}` }}>
              {d.short_description && (
                <div style={{ fontSize: 12, color: C.text, marginBottom: 6 }}>{d.short_description}</div>
              )}
              <div style={{ fontSize: 11, color: C.muted, display: 'flex', flexDirection: 'column', gap: 2 }}>
                {d.sector && <div>Category: {d.sector}</div>}
                {(d.country || d.headquarters) && (
                  <div>
                    Location: {[d.headquarters, d.country].filter(Boolean).join(', ')}
                  </div>
                )}
                {d.founded && <div>Founded: {d.founded}</div>}
                {d.edge_summary && (
                  <div>
                    {d.edge_summary.outgoing_count} investment{d.edge_summary.outgoing_count !== 1 ? 's' : ''} made
                    {d.edge_summary.outgoing_count > 0 && ` (${fmtSplitAmount(d.edge_summary.outgoing_confirmed_usd, d.edge_summary.outgoing_estimated_usd)})`}
                    {' · '}
                    {d.edge_summary.incoming_count} received
                    {d.edge_summary.incoming_count > 0 && ` (${fmtSplitAmount(d.edge_summary.incoming_confirmed_usd, d.edge_summary.incoming_estimated_usd)})`}
                  </div>
                )}
              </div>
              <button
                onClick={() => setEditing(e => !e)}
                style={{
                  marginTop: 8, padding: '3px 10px', fontSize: 11, cursor: 'pointer', borderRadius: 4,
                  background: editing ? `${C.public}22` : 'transparent',
                  border: `1px solid ${editing ? C.public : C.border}`,
                  color: editing ? C.public : C.text,
                }}
              >
                {editing ? 'Cancel edit' : 'Edit'}
              </button>

              {editing && (
                <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 8 }}>
                  <div>
                    <div style={{ fontSize: 10, color: C.muted, marginBottom: 3 }}>Type</div>
                    <select
                      value={form.type}
                      onChange={e => setForm(f => ({ ...f, type: e.target.value }))}
                      style={{
                        padding: '4px 8px', fontSize: 12, borderRadius: 4, outline: 'none',
                        border: `1px solid ${C.border}`, background: C.bg, color: C.text, width: '100%',
                      }}
                    >
                      {typeOptions.map(t => (
                        <option key={t} value={t}>{t.replace('_', ' ')}</option>
                      ))}
                    </select>
                  </div>
                  <div>
                    {/* multi-ticker list — replaces the old single
                        ticker text field. Each row is its own immediate
                        add/promote/remove request (see handle* functions
                        above), independent of the type/cik/sector Save
                        button below. */}
                    <div style={{ fontSize: 10, color: C.muted, marginBottom: 3 }}>
                      Tickers {tickerBusy && '· saving…'}
                    </div>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginBottom: 6 }}>
                      {tickers.length === 0 && (
                        <div style={{ fontSize: 11, color: C.muted }}>No ticker yet.</div>
                      )}
                      {tickers.map(t => (
                        <div key={t.id} style={{
                          display: 'flex', alignItems: 'center', gap: 6, fontSize: 12,
                          padding: '3px 6px', borderRadius: 4,
                          background: t.is_primary ? `${C.public}18` : 'transparent',
                          border: `1px solid ${t.is_primary ? C.public : C.border}`,
                        }}>
                          <span style={{ flex: 1, color: C.text }}>
                            {t.exchange ? `${t.exchange}: ${t.ticker}` : t.ticker}
                          </span>
                          {t.is_primary ? (
                            <span style={{ fontSize: 10, color: C.public }}>primary</span>
                          ) : (
                            <button
                              onClick={() => handleSetPrimaryTicker(t)}
                              disabled={tickerBusy}
                              style={{
                                background: 'transparent', border: `1px solid ${C.border}`, color: C.muted,
                                borderRadius: 3, padding: '1px 6px', fontSize: 10, cursor: 'pointer',
                              }}
                            >
                              Make primary
                            </button>
                          )}
                          <button
                            onClick={() => handleRemoveTicker(t)}
                            disabled={tickerBusy}
                            style={{
                              background: 'transparent', border: 'none', color: C.danger,
                              fontSize: 13, cursor: 'pointer', padding: '0 2px', lineHeight: 1,
                            }}
                            title="Remove ticker"
                          >
                            ×
                          </button>
                        </div>
                      ))}
                    </div>
                    <div style={{ display: 'flex', gap: 4 }}>
                      <input
                        value={newTicker}
                        onChange={e => setNewTicker(e.target.value)}
                        placeholder="e.g. AAPL or HKG: 9988"
                        style={{
                          flex: 1, padding: '4px 8px', fontSize: 12, borderRadius: 4, outline: 'none',
                          border: `1px solid ${C.border}`, background: C.bg, color: C.text,
                        }}
                      />
                      <button
                        onClick={handleAddTicker}
                        disabled={tickerBusy || !newTicker.trim()}
                        style={{
                          padding: '4px 10px', fontSize: 11, borderRadius: 4, cursor: 'pointer',
                          border: `1px solid ${C.border}`, background: 'transparent', color: C.text,
                        }}
                      >
                        Add
                      </button>
                    </div>
                    {tickerErr && <div style={{ fontSize: 11, color: C.danger, marginTop: 3 }}>{tickerErr}</div>}
                  </div>
                  <div>
                    <div style={{ fontSize: 10, color: C.muted, marginBottom: 3 }}>CIK</div>
                    <input
                      value={form.cik}
                      onChange={e => setForm(f => ({ ...f, cik: e.target.value }))}
                      placeholder="e.g. 0000320193"
                      style={{
                        padding: '4px 8px', fontSize: 12, borderRadius: 4, outline: 'none',
                        border: `1px solid ${C.border}`, background: C.bg, color: C.text, width: '100%',
                      }}
                    />
                  </div>
                  <div>
                    <div style={{ fontSize: 10, color: C.muted, marginBottom: 3 }}>Category (sector)</div>
                    <input
                      value={form.sector}
                      onChange={e => setForm(f => ({ ...f, sector: e.target.value }))}
                      placeholder="e.g. Technology"
                      style={{
                        padding: '4px 8px', fontSize: 12, borderRadius: 4, outline: 'none',
                        border: `1px solid ${C.border}`, background: C.bg, color: C.text, width: '100%',
                      }}
                    />
                  </div>
                  {saveErr && <div style={{ fontSize: 11, color: C.danger }}>{saveErr}</div>}
                  <button
                    onClick={handleSave}
                    disabled={saving}
                    style={{
                      padding: '5px 14px', fontSize: 12, cursor: saving ? 'default' : 'pointer', borderRadius: 4,
                      background: C.success, border: 'none', color: '#0f1117', fontWeight: 600,
                      opacity: saving ? 0.6 : 1,
                    }}
                  >
                    {saving ? 'Saving…' : 'Save'}
                  </button>
                </div>
              )}
            </div>

            {/* Price widget */}
            {d.ticker && (
              <div style={{ padding: '10px 16px', borderBottom: `1px solid ${C.border}` }}>
                <div style={{ display: 'flex', gap: 4, marginBottom: 8 }}>
                  {PRICE_RANGES.map(r => (
                    <button
                      key={r}
                      onClick={() => setRange(r)}
                      style={{
                        padding: '2px 8px', fontSize: 10, cursor: 'pointer', borderRadius: 3,
                        background: range === r ? `${C.public}22` : 'transparent',
                        border: `1px solid ${range === r ? C.public : C.border}`,
                        color: range === r ? C.public : C.muted,
                      }}
                    >
                      {r}
                    </button>
                  ))}
                </div>
                {priceLoading ? (
                  <div style={{ fontSize: 11, color: C.muted, padding: '20px 0', textAlign: 'center' }}>Loading…</div>
                ) : (
                  <>
                    <PriceChart points={price?.points} />
                    {price?.stale && (
                      <div style={{ fontSize: 10, color: C.muted, marginTop: 4 }}>
                        Showing last cached price (live fetch unavailable).
                      </div>
                    )}
                    {range === '1d' && price?.points?.length > 0 && (
                      <div style={{ fontSize: 10, color: C.muted, marginTop: 2 }}>
                        Intraday (5-min bars), most recent session.
                      </div>
                    )}
                  </>
                )}
              </div>
            )}

            {/* Filtered news */}
            <div>
              <div style={{ padding: '10px 16px 4px', fontSize: 11, fontWeight: 600, color: C.muted }}>
                NEWS
              </div>
              {news === null ? (
                <div style={{ padding: '0 16px 12px', color: C.muted, fontSize: 12 }}>Loading…</div>
              ) : news.length === 0 ? (
                <div style={{ padding: '0 16px 12px', color: C.muted, fontSize: 12 }}>No related news.</div>
              ) : (
                news.map(item => (
                  <div key={item.id} style={{ padding: '8px 16px', borderTop: `1px solid ${C.border}` }}>
                    <div style={{ display: 'flex', alignItems: 'flex-start', gap: 6, marginBottom: 3 }}>
                      <TierBadge tier={item.source_tier} />
                      <a
                        href={item.url}
                        target="_blank"
                        rel="noreferrer"
                        style={{ color: C.text, fontSize: 12, fontWeight: 500, lineHeight: 1.3, textDecoration: 'none' }}
                      >
                        {item.headline}
                      </a>
                    </div>
                    <div style={{ fontSize: 10, color: C.muted, paddingLeft: 30 }}>
                      {item.source_name} &middot; {item.published_at ? new Date(item.published_at).toLocaleDateString() : '—'}
                      {item.amount_usd != null && <> &middot; {fmtUsd(item.amount_usd)}</>}
                    </div>
                  </div>
                ))
              )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}

