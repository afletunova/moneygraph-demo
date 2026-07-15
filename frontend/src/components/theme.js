export const C = {
  bg: '#0f1117',
  surface: '#1a1d27',
  border: '#2a2d3a',
  text: '#e2e8f0',
  muted: '#64748b',
  public: '#3b82f6',
  private: '#f97316',
  dark_horse: '#a855f7',
  edge: '#4a5568',
  edgeHover: '#94a3b8',
  success: '#22c55e',
  warn: '#f59e0b',
  danger: '#ef4444',
}

export const NODE_COLOR = { public: C.public, private: C.private, dark_horse: C.dark_horse }


export const STATUS_COLOR = {
  running: C.warn, awaiting_harvest: C.warn, completed: C.success, failed: C.danger,
}
export const STATUS_LABEL = {
  running: 'running', awaiting_harvest: 'awaiting harvest',
  completed: 'completed', failed: 'failed',
}

