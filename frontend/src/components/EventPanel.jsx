import React, { useEffect, useState } from 'react'
import { C } from './theme'
import { fmtEdgeAmount, fmtUsd } from './formatters'

export default function EventPanel({ edge, onClose }) {
  const [events, setEvents] = useState(null)
  useEffect(() => {
    if (!edge) return
    setEvents(null)
    fetch(`/api/edges/${edge.id}/events`)
      .then(r => r.json())
      .then(setEvents)
      .catch(() => setEvents([]))
  }, [edge?.id])

  if (!edge) return null

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
        <div>
          <div style={{ fontSize: 13, fontWeight: 600, color: C.text, marginBottom: 2 }}>
            {edge.from_name} → {edge.to_name}
          </div>
          <div style={{ fontSize: 11, color: C.muted }}>
            {fmtEdgeAmount(edge)} &middot; {edge.source_count} source{edge.source_count !== 1 ? 's' : ''}
            {edge.last_confirmed && (
              <> &middot; confirmed {new Date(edge.last_confirmed).toLocaleDateString()}</>
            )}
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
        {events === null ? (
          <div style={{ padding: 16, color: C.muted, fontSize: 12 }}>Loading…</div>
        ) : events.length === 0 ? (
          <div style={{ padding: 16, color: C.muted, fontSize: 12 }}>No events recorded.</div>
        ) : (
          (() => {
            // A 'correction' row (delta_usd=0) reclassifies the
            // ORIGINAL event it points to (corrects_event_id) as effectively
            // estimated, without editing that original row (append-only).
            // Fold the two together in the UI: show ONE card per original
            // event, badge it using the correction's reason, and don't render
            // the $0 correction row as its own separate line (it would read
            // as a confusing zero-amount "investment").
            const correctionByTarget = {}
            events.forEach(ev => {
              if (ev.event_type === 'correction' && ev.corrects_event_id) {
                correctionByTarget[ev.corrects_event_id] = ev
              }
            })
            return events
              .filter(ev => ev.event_type !== 'correction')
              .map(ev => {
                const correction = correctionByTarget[ev.id]
                const effectiveStatus = correction ? 'estimated' : ev.value_status
                const effectiveReason = correction ? correction.estimate_reason : ev.estimate_reason
                return (
                  <div key={ev.id} style={{ padding: '10px 16px', borderBottom: `1px solid ${C.border}` }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 3 }}>
                      <span style={{ fontSize: 11, fontWeight: 600, color: C.text, textTransform: 'capitalize' }}>
                        {ev.event_type}
                      </span>
                      <span style={{ fontSize: 11, color: C.muted }}>
                        {ev.event_date?.split('T')[0]}
                      </span>
                    </div>
                    <div style={{ fontSize: 13, color: ev.delta_usd >= 0 ? C.success : C.danger, marginBottom: 2 }}>
                      {fmtUsd(ev.delta_usd)}
                      {effectiveStatus === 'estimated' && (
                        <span style={{
                          fontSize: 9, fontWeight: 600, color: C.warn, marginLeft: 6,
                          border: `1px solid ${C.warn}`, borderRadius: 3, padding: '1px 4px',
                        }}
                        title={
                          effectiveReason === 'syndicate_total'
                            ? 'Full syndicate-round total attributed to this investor — no per-investor breakdown was reported.'
                            : 'No numeric amount reported by the source.'
                        }>
                          {effectiveReason === 'syndicate_total' ? 'SYNDICATE TOTAL' : 'ESTIMATED'}
                        </span>
                      )}
                    </div>
                    <div style={{ fontSize: 10, color: C.muted, marginBottom: ev.raw_excerpt ? 2 : 0 }}>
                      Tier {ev.source_tier} &middot; {ev.confidence}
                      {ev.source_url && (
                        <>
                          {' &middot; '}
                          <a href={ev.source_url} target="_blank" rel="noreferrer"
                             style={{ color: C.public }}>
                            source
                          </a>
                        </>
                      )}
                    </div>
                    {ev.raw_excerpt && (
                      <div style={{
                        fontSize: 10, color: C.muted, fontStyle: 'italic', marginTop: 3,
                        overflow: 'hidden', display: '-webkit-box',
                        WebkitLineClamp: 2, WebkitBoxOrient: 'vertical',
                      }}>
                        {ev.raw_excerpt}
                      </div>
                    )}
                    {correction?.raw_excerpt && (
                      <div style={{ fontSize: 10, color: C.warn, fontStyle: 'italic', marginTop: 3 }}>
                        {correction.raw_excerpt}
                      </div>
                    )}
                  </div>
                )
              })
          })()
        )}
      </div>
    </div>
  )
}
