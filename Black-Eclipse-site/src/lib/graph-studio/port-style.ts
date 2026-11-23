// Port type → colour. One visual language for the whole editor so a port's
// colour always means the same type on every node and in the inspector.

import type { PortType, SideEffectClass } from './types';

const PORT_COLORS: Record<PortType, string> = {
  number: '#60a5fa',
  boolean: '#f472b6',
  string: '#34d399',
  object: '#a78bfa',
  array: '#c084fc',
  datetime: '#fbbf24',
  ticket: '#f59e0b',
  instrument: '#22d3ee',
  ohlcv: '#2dd4bf',
  indicator: '#818cf8',
  signal: '#f97316',
  portfolio: '#4ade80',
  order_intent: '#fb7185',
  research_finding: '#e879f9',
  evidence: '#facc15',
  agent_result: '#c084fc',
  execution_result: '#38bdf8',
  file: '#eab308',
  image: '#f472b6',
  any: '#94a3b8',
};

export function portColor(type: PortType): string {
  return PORT_COLORS[type] ?? '#94a3b8';
}

export function sideEffectColor(effect: SideEffectClass): string {
  switch (effect) {
    case 'trading': return '#fb7185';
    case 'write': return '#f59e0b';
    case 'local_write': return '#eab308';
    case 'model': return '#a78bfa';
    case 'network': return '#38bdf8';
    case 'read': return '#34d399';
    default: return '#64748b';
  }
}
