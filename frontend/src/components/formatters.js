export function fmtUsd(v) {
  if (!v) return '—'
  const abs = Math.abs(v)
  if (abs >= 1e9) return `$${(v / 1e9).toFixed(1)}B`
  if (abs >= 1e6) return `$${(v / 1e6).toFixed(0)}M`
  if (abs >= 1e3) return `$${(v / 1e3).toFixed(0)}K`
  return `$${v}`
}

// An edge's amount is shown as two figures — confirmed (sum of
// 'actual' events) + estimated (sum of 'estimated' events, e.g. a syndicate
// round total misattributed to one investor) — never collapsed into one net
// number. Falls back to the single net figure for edges with no estimated
// portion (the common case) or when the API hasn't sent the split fields.
export function fmtEdgeAmount(edge) {
  const estimated = edge.estimated_amount_usd || 0
  if (!estimated) return fmtUsd(edge.net_amount_usd)
  const confirmed = edge.confirmed_amount_usd || 0
  return `${fmtUsd(confirmed)} confirmed + ${fmtUsd(estimated)} est.`
}

// Same "confirmed + estimated, never collapsed" convention as
// fmtEdgeAmount, applied to a node panel's aggregate incoming/outgoing total
// (GET /nodes/{id}'s edge_summary.*_confirmed_usd/*_estimated_usd) — a blind
// sum across edges is exactly what let a node's total read as a physically
// implausible number when several edges shared an unflagged syndicate-round
// amount (confirmed live).
export function fmtSplitAmount(confirmed, estimated) {
  if (!estimated) return fmtUsd(confirmed)
  return `${fmtUsd(confirmed)} confirmed + ${fmtUsd(estimated)} est.`
}

// Per-share stock prices (from Yahoo Finance `close` values) are raw floats
// in the $10-$1000 range (e.g. 180.19000244140625, a float32 precision
// artifact) — distinct from fmtUsd's edge/investment amounts, which are
// always large round numbers. Round to cents; don't reuse fmtUsd here.
export function fmtPrice(v) {
  if (v == null || Number.isNaN(v)) return '—'
  return `$${v.toFixed(2)}`
}

export function fmtDuration(secs) {
  if (secs == null) return '—'
  if (secs < 60) return `${secs}s`
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  const s = secs % 60
  if (h > 0) return `${h}h ${m}m`
  return `${m}m ${s}s`
}
