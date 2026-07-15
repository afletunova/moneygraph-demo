import React from 'react'
import { C, STATUS_COLOR, STATUS_LABEL } from './theme'

export default function Header({ run }) {
  const st = run ? (run.display_status ?? run.status) : null
  return (
    <header style={{
      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      padding: '10px 20px', background: C.surface, borderBottom: `1px solid ${C.border}`,
      flexShrink: 0,
    }}>
      <span style={{ fontWeight: 700, fontSize: 15, color: C.text, letterSpacing: '0.01em' }}>
        AI Investment Graph
      </span>
      <span style={{ fontSize: 12, color: C.muted }}>
        {run ? (
          <>
            Last run:{' '}
            <span style={{ color: STATUS_COLOR[st] ?? C.muted }}>{STATUS_LABEL[st] ?? st}</span>
            {run.completed_at && (
              <> &middot; {new Date(run.completed_at).toLocaleString()}</>
            )}
            {run.edges_created > 0 && (
              <> &middot; {run.edges_created} edges</>
            )}
          </>
        ) : (
          'No pipeline runs'
        )}
      </span>
    </header>
  )
}

