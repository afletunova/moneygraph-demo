import React, { useEffect, useRef, useState } from 'react'
import * as d3 from 'd3'
import { C, NODE_COLOR } from '../components/theme'
import { fmtEdgeAmount } from '../components/formatters'
import Tooltip from '../components/Tooltip'
import EventPanel from '../components/EventPanel'
import NodePanel from '../components/NodePanel'
import Legend from '../components/Legend'
import FilterBar from '../components/FilterBar'

function nodeR(d, deg) {
  return Math.max(8, 5 + (deg[d.id] || 0) * 2)
}


// Highlight-and-dim filter, not hide/show. `nodeMatches` is the single
// source of truth for what counts as a "match" so the node fill/opacity and
// the edge dim rule below can't drift apart. Empty string ('') means that
// dimension has no active filter. Dimensions AND-combine (all active
// dimensions must match) — kept to 3 dimensions max so this stays simple.
function nodeMatches(d, filters) {
  if (filters.sector && d.sector !== filters.sector) return false
  if (filters.country && d.country !== filters.country) return false
  if (filters.type && d.type !== filters.type) return false
  // A node's "exchange" for filtering purposes = its primary
  // ticker's exchange (see get_current_graph's NULLIF(nt.exchange, '')).
  // Nodes with no ticker/exchange have d.exchange falsy and simply never
  // match any exchange filter value — same "missing dimension never
  // matches" behaviour sector/country already have.
  if (filters.exchange && d.exchange !== filters.exchange) return false
  return true
}

// Edge-dim rule — an edge keeps its normal colour only when BOTH
// endpoints match the active filter; if either endpoint fails to match, the
// edge dims. Chosen over "dims only if both endpoints fail" because the goal
// is to make the highlighted subset visually pop as a clean subgraph — an
// edge from a matching node to a non-matching one is still "leaving" the
// highlighted set, so it reads as dimmed background, not part of the
// highlight.
function applyFilters(sel, filters) {
  const { nodeSel, linkSel } = sel
  if (!nodeSel || !linkSel) return
  const active = !!(filters.sector || filters.country || filters.type || filters.exchange)

  nodeSel.select('circle')
    .attr('fill', d => (!active || nodeMatches(d, filters)) ? (NODE_COLOR[d.type] ?? '#888') : C.muted)
    .attr('opacity', d => (!active || nodeMatches(d, filters)) ? 1 : 0.35)

  nodeSel.select('text')
    .attr('opacity', d => (!active || nodeMatches(d, filters)) ? 1 : 0.35)

  linkSel
    .attr('stroke', d => {
      if (!active) return C.edge
      const bothMatch = nodeMatches(d.source, filters) && nodeMatches(d.target, filters)
      return bothMatch ? C.edge : C.muted
    })
    .attr('stroke-opacity', d => {
      if (!active) return 0.75
      const bothMatch = nodeMatches(d.source, filters) && nodeMatches(d.target, filters)
      return bothMatch ? 0.75 : 0.12
    })
}

export default function GraphView({ nodes, edges, onGraphRefresh }) {
  const svgRef = useRef(null)
  const [tip, setTip] = useState(null)
  const [selectedEdge, setSelectedEdge] = useState(null)
  const [selectedNode, setSelectedNode] = useState(null)
  const [filters, setFilters] = useState({ sector: '', country: '', type: '', exchange: '' })
  const selRef = useRef({ nodeSel: null, linkSel: null })

  useEffect(() => {
    if (!svgRef.current || !nodes.length) return
    const el = svgRef.current
    const width = el.clientWidth || 1200
    const height = el.clientHeight || 700

    const deg = {}
    nodes.forEach(n => { deg[n.id] = 0 })
    edges.forEach(e => {
      deg[e.from_node_id] = (deg[e.from_node_id] || 0) + 1
      deg[e.to_node_id] = (deg[e.to_node_id] || 0) + 1
    })

    const amounts = edges.map(e => e.net_amount_usd).filter(a => a > 0)
    const minAmt = amounts.length ? Math.min(...amounts) : 1
    const maxAmt = amounts.length ? Math.max(...amounts) : 1
    const thickScale = minAmt < maxAmt
      ? d3.scaleLog().domain([minAmt, maxAmt]).range([1.5, 8]).clamp(true)
      : () => 3
    const edgeW = d => d.net_amount_usd > 0 ? thickScale(d.net_amount_usd) : 1

    const simNodes = nodes.map(n => ({ ...n }))
    const simLinks = edges.map(e => ({ ...e, source: e.from_node_id, target: e.to_node_id }))

    const svg = d3.select(el)
    svg.selectAll('*').remove()

    const defs = svg.append('defs')
    ;['arrow', 'arrow-hover'].forEach(id => {
      defs.append('marker')
        .attr('id', id)
        .attr('viewBox', '0 -5 10 10')
        .attr('refX', 8).attr('refY', 0)
        .attr('markerWidth', 5).attr('markerHeight', 5)
        .attr('orient', 'auto')
        .append('path')
        .attr('d', 'M0,-5L10,0L0,5')
        .attr('fill', id === 'arrow' ? C.edge : C.edgeHover)
    })

    const g = svg.append('g')
    svg.call(
      d3.zoom().scaleExtent([0.05, 8])
        .on('zoom', ev => g.attr('transform', ev.transform))
    )

    const sim = d3.forceSimulation(simNodes)
      .force('link', d3.forceLink(simLinks).id(d => d.id).distance(160))
      .force('charge', d3.forceManyBody().strength(-450))
      .force('center', d3.forceCenter(width / 2, height / 2))
      .force('collision', d3.forceCollide().radius(d => nodeR(d, deg) + 10))

    const linkSel = g.append('g')
      .selectAll('line')
      .data(simLinks)
      .join('line')
      .attr('stroke', C.edge)
      .attr('stroke-width', edgeW)
      .attr('stroke-opacity', 0.75)
      .attr('marker-end', 'url(#arrow)')
      .style('cursor', 'pointer')
      .on('mousemove', (ev, d) => {
        const confirmed = d.last_confirmed
          ? new Date(d.last_confirmed).toLocaleDateString()
          : '—'
        setTip({
          x: ev.clientX, y: ev.clientY,
          content: (
            <div>
              <div style={{ fontWeight: 600, marginBottom: 4, color: C.text }}>
                {d.from_name} → {d.to_name}
              </div>
              <div style={{ marginBottom: 2 }}>{fmtEdgeAmount(d)}</div>
              <div style={{ color: C.muted }}>
                {d.source_count} source{d.source_count !== 1 ? 's' : ''}
              </div>
              <div style={{ color: C.muted }}>Last confirmed {confirmed}</div>
              <div style={{ marginTop: 6, color: C.public, fontSize: 10 }}>
                Click to see events
              </div>
            </div>
          ),
        })
      })
      .on('mouseleave', () => setTip(null))
      .on('click', (ev, d) => {
        ev.stopPropagation()
        setTip(null)
        setSelectedNode(null)
        setSelectedEdge(d)
      })

    const nodeSel = g.append('g')
      .selectAll('g')
      .data(simNodes)
      .join('g')
      .style('cursor', 'grab')
      .call(
        d3.drag()
          .on('start', (ev, d) => {
            if (!ev.active) sim.alphaTarget(0.3).restart()
            d.fx = d.x; d.fy = d.y
          })
          .on('drag', (ev, d) => { d.fx = ev.x; d.fy = ev.y })
          .on('end', (ev, d) => {
            if (!ev.active) sim.alphaTarget(0)
            d.fx = null; d.fy = null
          })
      )
      .on('mousemove', (ev, d) => {
        setTip({
          x: ev.clientX, y: ev.clientY,
          content: (
            <div>
              <div style={{ fontWeight: 600, marginBottom: 3 }}>{d.name}</div>
              {d.ticker && <div style={{ color: C.muted, marginBottom: 1 }}>{d.ticker}</div>}
              <div style={{ textTransform: 'capitalize', color: NODE_COLOR[d.type] ?? C.muted }}>
                {d.type?.replace('_', ' ')}
              </div>
              {(d.short_description || d.sector) && (
                <div style={{ color: C.muted, marginTop: 4, maxWidth: 240 }}>
                  {d.short_description}
                  {d.short_description && d.sector && ' · '}
                  {d.sector}
                </div>
              )}
            </div>
          ),
        })
      })
      .on('mouseleave', () => setTip(null))
      .on('click', (ev, d) => {
        ev.stopPropagation()
        setTip(null)
        setSelectedEdge(null)
        setSelectedNode(d)
      })

    nodeSel.append('circle')
      .attr('r', d => nodeR(d, deg))
      .attr('fill', d => NODE_COLOR[d.type] ?? '#888')
      .attr('stroke', C.surface)
      .attr('stroke-width', 2)

    nodeSel.append('text')
      .text(d => d.ticker || d.name.split(' ')[0])
      .attr('text-anchor', 'middle')
      .attr('dy', d => nodeR(d, deg) + 13)
      .attr('font-size', 10)
      .attr('fill', C.muted)
      .style('pointer-events', 'none')
      .style('user-select', 'none')

    sim.on('tick', () => {
      linkSel
        .attr('x1', d => d.source.x)
        .attr('y1', d => d.source.y)
        .attr('x2', d => {
          const dx = d.target.x - d.source.x
          const dy = d.target.y - d.source.y
          const dist = Math.sqrt(dx * dx + dy * dy) || 1
          return d.target.x - (dx / dist) * (nodeR(d.target, deg) + 7)
        })
        .attr('y2', d => {
          const dx = d.target.x - d.source.x
          const dy = d.target.y - d.source.y
          const dist = Math.sqrt(dx * dx + dy * dy) || 1
          return d.target.y - (dy / dist) * (nodeR(d.target, deg) + 7)
        })
      nodeSel.attr('transform', d => `translate(${d.x},${d.y})`)
    })

    // Selections persisted so the filter effect below can restyle
    // fill/opacity without tearing down and restarting the simulation (that
    // would jump the layout on every dropdown change).
    selRef.current = { nodeSel, linkSel }
    applyFilters(selRef.current, filters)

    return () => sim.stop()
  }, [nodes, edges])

  // Applies the current highlight-and-dim filter to the existing
  // selections. Separate from the layout effect above on purpose — changing
  // a filter must never re-run the force simulation.
  useEffect(() => {
    applyFilters(selRef.current, filters)
  }, [filters])

  return (
    <div style={{ flex: 1, position: 'relative', overflow: 'hidden' }}>
      <svg ref={svgRef} style={{ width: '100%', height: '100%', display: 'block' }} />
      <FilterBar nodes={nodes} filters={filters} onChange={setFilters} />
      <Legend />
      <Tooltip tip={tip} />
      {nodes.length === 0 && (
        <div style={{
          position: 'absolute', inset: 0, display: 'flex', alignItems: 'center',
          justifyContent: 'center', color: C.muted, fontSize: 14, pointerEvents: 'none',
        }}>
          Loading graph…
        </div>
      )}
      {selectedEdge && (
        <EventPanel edge={selectedEdge} onClose={() => setSelectedEdge(null)} />
      )}
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
