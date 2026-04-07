import pluginJson from './plugin.json';

export const PLUGIN_BASE_URL = `/a/${pluginJson.id}`;
export const PLUGIN_ID = pluginJson.id;

// Backend URLs — auto-detect based on current hostname.
// Local dev: Grafana on :3001, agent on :8000 (separate ports)
// Deployed:  Grafana on :3000, agent on :8000 (same host)
const host = window.location.hostname;
const isLocal = host === 'localhost' || host === '127.0.0.1';
const agentHost = isLocal ? 'localhost:8000' : `${host}:8000`;
const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';

export const WS_URL = `${wsProtocol}//${agentHost}/ws`;
export const API_URL = `${window.location.protocol}//${agentHost}`;
