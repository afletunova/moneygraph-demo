import React, { useEffect, useState } from 'react'
import { C, STATUS_COLOR, STATUS_LABEL } from '../components/theme'
import { fmtDuration } from '../components/formatters'

const RUN_TYPE_COLOR = {
  edgar: C.public, rss: C.dark_horse, websearch: C.warn, legacy: C.muted, reresolve: C.private,
}

export default function RunsView() {
  const [runs, setRuns] = useState([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(null)
  const [toast, setToast] = useState(null)

  const load = () =>
    fetch('/api/pipeline/runs?limit=30')
      .then(r => r.json())
      .then(d => { setRuns(d.runs || []); setLoading(false) })
      .catch(() => setLoading(false))

  useEffect(() => {
    load()
    const id = setInterval(load, 5000)  // batch runs flip status async — keep fresh
    return () => clearInterval(id)
  }, [])

  const trigger = (label, path) => {
    setBusy(label)
    fetch(path, { method: 'POST' })
      .then(r => r.json())
      .then(() => { setToast(`${label} started`); load() })
      .catch(() => setToast(`${label} failed to start`))
      .finally(() => { setBusy(null); setTimeout(() => setToast(null), 3000) })
  }

  const harvest = () => {
    setBusy('Harvest')
    fetch('/api/pipeline/harvest', { method: 'POST' })
      .then(r => r.json())
      .then(s => { setToast(`Harvest: ${s.runs_harvested ?? 0} harvested, ${s.runs_pending ?? 0} pending`); load() })
      .catch(() => setToast('Harvest failed'))
      .finally(() => { setBusy(null); setTimeout(() => setToast(null), 4000) })
  }

  const actions = [
    ['EDGAR run', () => trigger('EDGAR run', '/api/pipeline/run')],
    ['RSS run', () => trigger('RSS run', '/api/pipeline/rss')],
    ['Web search', () => trigger('Web search', '/api/pipeline/websearch')],
    ['Harvest batches', harvest],
    ['Re-resolve', () => trigger('Re-resolve', '/api/pipeline/reresolve')],
  ]

  const btn = {
    padding: '7px 14px', border: `1px solid ${C.border}`, borderRadius: 6,
    background: C.surface, color: C.text, fontSize: 13, cursor: 'pointer',
  }
  const th = { textAlign: 'left', padding: '6px 10px', color: C.muted, fontWeight: 600, fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.03em' }
  const td = { padding: '6px 10px', fontSize: 12, color: C.text, borderTop: `1px solid ${C.border}` }

  return (
    <div style={{ padding: 24, overflow: 'auto' }}>
      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 8 }}>
        {actions.map(([label, fn]) => (
          <button key={label} onClick={fn} disabled={busy !== null} style={{
            ...btn, opacity: busy !== null ? 0.5 : 1,
            cursor: busy !== null ? 'default' : 'pointer',
          }}>
            {busy === label ? `${label}…` : label}
          </button>
        ))}
      </div>
      <div style={{ fontSize: 11, color: C.muted, marginBottom: 16 }}>
        Harvest polls OpenAI batch runs and writes results; it also runs automatically every 15&nbsp;min.
        Re-resolve recovers edges from existing news whose companies just got approved; it also runs automatically after every EDGAR run.
      </div>
      {toast && (
        <div style={{ marginBottom: 12, fontSize: 12, color: C.success }}>{toast}</div>
      )}

      {loading ? (
        <div style={{ color: C.muted, fontSize: 13 }}>Loading…</div>
      ) : runs.length === 0 ? (
        <div style={{ color: C.muted, fontSize: 13 }}>No pipeline runs yet.</div>
      ) : (
        <table style={{ borderCollapse: 'collapse', width: '100%', maxWidth: 900 }}>
          <thead>
            <tr>
              <th style={th}>Started</th>
              <th style={th}>Completed</th>
              <th style={th}>Duration</th>
              <th style={th}>Type</th>
              <th style={th}>Status</th>
              <th style={th}>Progress</th>
              <th style={th}>Nodes</th>
              <th style={th}>Edges</th>
              <th style={th}>Candidates</th>
              <th style={th}>Events</th>
              <th style={th}>Failed rows</th>
              <th style={th}>Est. cost</th>
            </tr>
          </thead>
          <tbody>
            {runs.map(r => (
              <tr key={r.id}>
                <td style={td}>{r.started_at ? new Date(r.started_at).toLocaleString() : '—'}</td>
                <td style={td}>{r.completed_at ? new Date(r.completed_at).toLocaleString() : '—'}</td>
                <td style={{ ...td, color: r.completed_at ? C.text : C.warn }}>
                  {fmtDuration(r.duration_seconds)}{r.completed_at ? '' : ' …'}
                </td>
                <td style={{ ...td, color: RUN_TYPE_COLOR[r.run_type] ?? C.muted }}>{r.run_type}</td>
                <td style={{ ...td, color: STATUS_COLOR[r.display_status] ?? C.muted }}>
                  {STATUS_LABEL[r.display_status] ?? r.display_status}
                  {r.display_status === 'running' && (
                    <span style={{ color: C.muted }}> &middot; live</span>
                  )}
                  {r.display_status === 'failed' && r.error_message && (
                    <span title={r.error_message} style={{ color: C.muted }}> &#9432;</span>
                  )}
                </td>
                <td style={td}>
                  {/* percent-complete + ETA, running rows only — total_units
                      unknown/0 or too few samples yet means nothing to show, not a
                      fabricated 0%/nonsense ETA (see api _run_progress). */}
                  {r.display_status === 'running' && r.percent_complete != null ? (
                    <>
                      {r.percent_complete}%
                      {r.eta_seconds != null && (
                        <span style={{ color: C.muted }}> &middot; ~{fmtDuration(r.eta_seconds)} left</span>
                      )}
                    </>
                  ) : '—'}
                </td>
                <td style={td}>{r.nodes_processed}</td>
                <td style={td}>{r.edges_created}</td>
                <td style={td}>{r.candidates_found}</td>
                <td style={td}>{r.events_logged}</td>
                <td style={{ ...td, color: r.failed_rows > 0 ? C.danger : C.muted }}>
                  {r.failed_rows > 0 ? r.failed_rows : '—'}
                </td>
                <td style={td}>
                  {/* search-tool-fee estimate ($0.025/search call), websearch runs only. */}
                  {r.run_type === 'websearch' ? `$${r.est_cost_usd.toFixed(3)}` : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
