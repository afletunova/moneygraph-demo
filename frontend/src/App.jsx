import React, { useEffect, useState } from 'react'
import { C } from './components/theme'
import Header from './components/Header'
import TabBar from './components/TabBar'
import GraphView from './views/GraphView'
import NodesView from './views/NodesView'
import NewsView from './views/NewsView'
import ReviewQueueView from './views/ReviewQueueView'
import RunsView from './views/RunsView'
import SettingsView from './views/SettingsView'

function useGraphData() {
  const [data, setData] = useState({ nodes: [], edges: [] })
  const load = () => fetch('/api/graph/current').then(r => r.json()).then(setData).catch(() => {})
  useEffect(() => { load() }, [])
  // Exposed so the node-detail panel can refresh graph state (color/
  // tooltip fields) after an inline edit (type/ticker/sector) without a full
  // page reload.
  return { ...data, refetch: load }
}

function usePipelineLatest() {
  const [run, setRun] = useState(null)
  useEffect(() => {
    const poll = () =>
      fetch('/api/pipeline/latest')
        .then(r => r.status === 404 ? null : r.json())
        .then(d => { if (d) setRun(d) })
        .catch(() => {})
    poll()
    const id = setInterval(poll, 5000)
    return () => clearInterval(id)
  }, [])
  return run
}

export default function App() {
  const [activeTab, setActiveTab] = useState('Graph')
  const { nodes, edges, refetch } = useGraphData()
  const run = usePipelineLatest()

  return (
    <div style={{
      display: 'flex', flexDirection: 'column', height: '100vh',
      background: C.bg, fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
      color: C.text,
    }}>
      <Header run={run} />
      <TabBar active={activeTab} onChange={setActiveTab} />
      <div style={{ flex: 1, overflow: 'hidden', position: 'relative', display: 'flex', flexDirection: 'column' }}>
        {activeTab === 'Graph' && <GraphView nodes={nodes} edges={edges} onGraphRefresh={refetch} />}
        {activeTab === 'Nodes' && <NodesView onGraphRefresh={refetch} />}
        {activeTab === 'News' && <NewsView />}
        {activeTab === 'Review Queue' && <ReviewQueueView />}
        {activeTab === 'Runs' && <RunsView />}
        {activeTab === 'Settings' && <SettingsView />}
      </div>
    </div>
  )
}
