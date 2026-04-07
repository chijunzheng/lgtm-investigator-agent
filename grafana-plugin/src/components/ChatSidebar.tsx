import React, { useState, useEffect, useRef, useCallback } from 'react';
import { css } from '@emotion/css';
import { GrafanaTheme2 } from '@grafana/data';
import { Button, Input, useStyles2, Spinner } from '@grafana/ui';
import { WS_URL, API_URL } from '../constants';
import { InlinePanel } from './InlinePanel';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface AgentEvent {
  type: 'reasoning' | 'tool_start' | 'tool_result' | 'panel_add' | 'done' | 'topology' | 'error' | 'reset_ack';
  content?: string;
  tool?: string;
  args?: string;
  query?: string;
  duration?: number;
  cached?: boolean;
  error?: boolean;
  trace?: Array<Record<string, unknown>>;
  stats?: Record<string, unknown>;
  message?: string;
  panel_type?: string;
  title?: string;
  datasource_uid?: string;
  time_from?: string;
  time_to?: string;
}

interface DemoScenario {
  name: string;
  description: string;
  symptom: string;
  time_window: { start: string; end: string };
}

interface StreamItem {
  id: number;
  type: 'user' | 'agent' | 'tool' | 'panel' | 'diagnosis' | 'system';
  content: string;
  // tool fields
  toolName?: string;
  duration?: number;
  cached?: boolean;
  error?: boolean;
  // panel fields
  panelType?: string;
  title?: string;
  datasourceUid?: string;
  query?: string;
  timeFrom?: string;
  timeTo?: string;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

let msgId = 0;

const DIAGNOSIS_SECTIONS = [
  'Root Cause:', 'Confidence:', 'Evidence:',
  'Contradictions:', 'Contradicting Evidence:',
  'Not Investigated:', 'Remediation:',
];

function renderMarkdown(text: string): string {
  let html = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
  // Strip ```diagnosis fences and render contents with section headers
  html = html.replace(/```diagnosis\n?([\s\S]*?)```/g, (_match, inner: string) => {
    let d = inner;
    for (const section of DIAGNOSIS_SECTIONS) {
      d = d.replaceAll(section, `<span class="dx-section">${section}</span>`);
    }
    d = d.replace(/`([^`]+)`/g, '<code>$1</code>');
    d = d.replace(/^(\s*)[-•]\s+/gm, '$1<span class="dx-bullet">-</span> ');
    d = d.replace(/\n/g, '<br/>');
    return `<div class="inline-diagnosis">${d}</div>`;
  });
  // Regular code blocks
  html = html.replace(/```[\w]*\n?([\s\S]*?)```/g, '<pre><code>$1</code></pre>');
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/^(\s*)[-•]\s+/gm, '$1<span class="chat-bullet">-</span> ');
  html = html.replace(/\n/g, '<br/>');
  return html;
}

function diagnosisToHtml(raw: string): string {
  let text = raw.replace(/```diagnosis/g, '').replace(/```/g, '').trim();
  text = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  for (const section of DIAGNOSIS_SECTIONS) {
    text = text.replaceAll(section, `<span class="dx-section">${section}</span>`);
  }
  text = text.replace(/`([^`]+)`/g, '<code>$1</code>');
  text = text.replace(/^(\s*)[-•]\s+/gm, '$1<span class="dx-bullet">-</span> ');
  text = text.replace(/\n/g, '<br/>');
  return text;
}

// ---------------------------------------------------------------------------
// Streaming Diagnosis sub-component
// ---------------------------------------------------------------------------

function DiagnosisBlock({ content }: { content: string }) {
  const s = useStyles2(getStyles);
  const html = diagnosisToHtml(content);

  return (
    <div className={s.diagnosisContainer}>
      <div className={s.diagnosisHeader}>Diagnosis</div>
      <div className={s.diagnosisBody} dangerouslySetInnerHTML={{ __html: html }} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function ChatSidebar() {
  const s = useStyles2(getStyles);
  const [items, setItems] = useState<StreamItem[]>([]);
  const [input, setInput] = useState('');
  const [isConnected, setIsConnected] = useState(false);
  const [isInvestigating, setIsInvestigating] = useState(false);
  const [demos, setDemos] = useState<DemoScenario[]>([]);
  const wsRef = useRef<WebSocket | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const pendingReasoningRef = useRef('');
  const autoScrollRef = useRef(true);
  const lastScrollTopRef = useRef(0);
  const programmaticScrollRef = useRef(false);

  // Scroll to bottom (unconditional — caller checks autoScrollRef)
  const scrollToBottom = useCallback(() => {
    const el = scrollRef.current;
    if (!el || !autoScrollRef.current) { return; }
    programmaticScrollRef.current = true;
    el.scrollTop = el.scrollHeight;
  }, []);

  // Detect user scrolling UP to disable auto-scroll; scrolling to bottom re-enables
  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) { return; }
    // Ignore scroll events we triggered ourselves
    if (programmaticScrollRef.current) {
      programmaticScrollRef.current = false;
      lastScrollTopRef.current = el.scrollTop;
      return;
    }
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 50;
    if (el.scrollTop < lastScrollTopRef.current - 10) {
      // User scrolled UP → pause auto-scroll
      autoScrollRef.current = false;
    } else if (atBottom) {
      // User scrolled back to bottom → resume
      autoScrollRef.current = true;
    }
    lastScrollTopRef.current = el.scrollTop;
  }, []);

  // Scroll when items change
  useEffect(() => { scrollToBottom(); }, [items, scrollToBottom]);

  // MutationObserver for async panel renders
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) { return; }
    const obs = new MutationObserver(() => {
      if (autoScrollRef.current) {
        programmaticScrollRef.current = true;
        el.scrollTop = el.scrollHeight;
      }
    });
    obs.observe(el, { childList: true, subtree: true });
    return () => obs.disconnect();
  }, []);

  // Fetch demos
  useEffect(() => {
    fetch(`${API_URL}/api/demos`).then((r) => r.json()).then(setDemos).catch(() => {});
  }, []);

  // WebSocket
  useEffect(() => {
    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;
    ws.onopen = () => setIsConnected(true);
    ws.onclose = () => { setIsConnected(false); setIsInvestigating(false); };
    ws.onmessage = (evt) => handleEvent(JSON.parse(evt.data));
    return () => ws.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleEvent = useCallback((event: AgentEvent) => {
    switch (event.type) {
      case 'reasoning': {
        pendingReasoningRef.current += event.content ?? '';
        setItems((prev) => {
          const last = prev[prev.length - 1];
          if (last && last.type === 'agent' && last.id === -1) {
            return [...prev.slice(0, -1), { ...last, content: pendingReasoningRef.current }];
          }
          return [...prev, { id: -1, type: 'agent', content: pendingReasoningRef.current }];
        });
        break;
      }
      case 'tool_start':
        flushReasoning();
        setItems((prev) => [
          ...prev,
          { id: ++msgId, type: 'tool', content: `Querying ${event.tool}...`, toolName: event.tool },
        ]);
        break;

      case 'tool_result':
        setItems((prev) => {
          const idx = [...prev].reverse().findIndex((m) => m.type === 'tool' && m.toolName === event.tool && !m.duration);
          if (idx >= 0) {
            const realIdx = prev.length - 1 - idx;
            const updated = {
              ...prev[realIdx],
              content: `${event.tool} ${event.cached ? '[cached]' : ''} ${event.error ? 'ERROR' : ''}`,
              duration: event.duration,
              cached: event.cached,
              error: event.error,
            };
            return [...prev.slice(0, realIdx), updated, ...prev.slice(realIdx + 1)];
          }
          return prev;
        });
        break;

      case 'panel_add':
        setItems((prev) => [
          ...prev,
          {
            id: ++msgId,
            type: 'panel',
            content: '',
            panelType: event.panel_type,
            title: event.title,
            datasourceUid: event.datasource_uid,
            query: event.query,
            timeFrom: event.time_from,
            timeTo: event.time_to,
          },
        ]);
        break;

      case 'done':
        flushReasoning();
        setIsInvestigating(false);
        if (event.content) {
          const isDiagnosis = event.content.includes('Root Cause:') || event.content.includes('Confidence:');
          // Only add as a new item if it's a diagnosis block.
          // Non-diagnosis content was already shown via reasoning events.
          if (isDiagnosis) {
            setItems((prev) => [...prev, { id: ++msgId, type: 'diagnosis', content: event.content ?? '' }]);
          }
        }
        if (event.trace && event.stats) {
          const stats = event.stats as Record<string, unknown>;
          const cost = (stats.cost as Record<string, unknown>) ?? {};
          const timeline = `Investigation complete | Tools: ${stats.total_tool_calls} | Cost: $${Number(cost.estimated_cost ?? 0).toFixed(4)}`;
          setItems((prev) => [...prev, { id: ++msgId, type: 'system', content: timeline }]);
        }
        break;

      case 'error':
        setIsInvestigating(false);
        setItems((prev) => [...prev, { id: ++msgId, type: 'system', content: `Error: ${event.message}` }]);
        break;

      default:
        break;
    }
  }, []);

  function flushReasoning() {
    if (pendingReasoningRef.current) {
      setItems((prev) => {
        const last = prev[prev.length - 1];
        if (last && last.id === -1) {
          return [...prev.slice(0, -1), { ...last, id: ++msgId }];
        }
        return prev;
      });
      pendingReasoningRef.current = '';
    }
  }

  const lastTimeWindowRef = useRef<{ start: string; end: string } | null>(null);

  function sendInvestigation(symptom: string, timeWindow?: { start: string; end: string }) {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) { return; }
    if (timeWindow) {
      lastTimeWindowRef.current = timeWindow;
    }
    // Resolve time window: explicit > last used > widest range across all demos
    let tw = timeWindow ?? lastTimeWindowRef.current;
    if (!tw && demos.length > 0) {
      const starts = demos.map((d) => d.time_window.start).sort();
      const ends = demos.map((d) => d.time_window.end).sort();
      tw = { start: starts[0], end: ends[ends.length - 1] };
    }
    setIsInvestigating(true);
    autoScrollRef.current = true;
    pendingReasoningRef.current = '';
    setItems((prev) => [...prev, { id: ++msgId, type: 'user', content: symptom }]);
    wsRef.current.send(JSON.stringify({ type: 'investigate', symptom, time_window: tw }));
  }

  function handleSubmit() {
    if (!input.trim() || isInvestigating) { return; }
    sendInvestigation(input.trim());
    setInput('');
  }

  function handleReset() {
    wsRef.current?.send(JSON.stringify({ type: 'reset' }));
    setItems([]);
    setIsInvestigating(false);
    pendingReasoningRef.current = '';
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSubmit(); }
  }

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------
  return (
    <div className={s.page}>
      {/* Header bar */}
      <div className={s.topBar}>
        <span className={s.headerTitle}>Investigate Agent</span>
        <span className={isConnected ? s.statusOnline : s.statusOffline}>
          {isConnected ? 'Connected' : 'Disconnected'}
        </span>
        {demos.length > 0 && (
          <div className={s.demos}>
            {demos.map((d) => (
              <Button key={d.name} size="sm" variant="secondary" disabled={isInvestigating}
                onClick={() => sendInvestigation(d.symptom, d.time_window)} title={d.description}>
                {d.name}
              </Button>
            ))}
          </div>
        )}
      </div>

      {/* Stream */}
      <div className={s.stream} ref={scrollRef} onScroll={handleScroll}>
        {items.length === 0 && (
          <div className={s.emptyState}>
            Describe an incident symptom to start investigating, or click a scenario above.
          </div>
        )}

        {items.map((item) => {
          switch (item.type) {
            case 'user':
              return <div key={item.id} className={s.userMsg}>{item.content}</div>;

            case 'agent':
              return (
                <div key={item.id} className={s.agentMsg}
                  dangerouslySetInnerHTML={{ __html: renderMarkdown(item.content) }} />
              );

            case 'tool':
              return (
                <div key={item.id} className={s.toolCard}>
                  <span className={s.toolIcon}>{item.error ? '✗' : item.duration ? '✓' : '⟳'}</span>
                  <span className={s.toolName}>{item.toolName}</span>
                  {item.duration != null && <span className={s.toolDuration}>{item.duration}s</span>}
                  {item.cached && <span className={s.toolCached}>cached</span>}
                </div>
              );

            case 'panel':
              return (
                <div key={item.id} className={s.panelRow}>
                  <InlinePanel
                    panelType={item.panelType!}
                    title={item.title!}
                    datasourceUid={item.datasourceUid!}
                    query={item.query!}
                    timeFrom={item.timeFrom!}
                    timeTo={item.timeTo!}
                  />
                </div>
              );

            case 'diagnosis':
              return <DiagnosisBlock key={item.id} content={item.content} />;

            case 'system':
              return <div key={item.id} className={s.systemMsg}>{item.content}</div>;

            default:
              return null;
          }
        })}

        {isInvestigating && (
          <div className={s.systemMsg}><Spinner size="sm" /> Investigating...</div>
        )}
      </div>

      {/* Input */}
      <div className={s.inputArea}>
        <Input value={input} onChange={(e) => setInput(e.currentTarget.value)}
          onKeyDown={handleKeyDown}
          placeholder={isInvestigating ? 'Investigating...' : 'Describe the incident...'}
          disabled={isInvestigating || !isConnected} />
        <div className={s.inputButtons}>
          <Button onClick={handleSubmit} disabled={isInvestigating || !input.trim()}>Send</Button>
          <Button variant="secondary" onClick={handleReset} disabled={isInvestigating}>Reset</Button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

function getStyles(theme: GrafanaTheme2) {
  return {
    page: css`
      display: flex;
      flex-direction: column;
      height: calc(100vh - 80px);
      width: 100%;
      max-width: 1200px;
      margin: 0 auto;
    `,

    // Top bar
    topBar: css`
      display: flex;
      align-items: center;
      gap: ${theme.spacing(1.5)};
      padding: ${theme.spacing(1.5)} ${theme.spacing(2)};
      border-bottom: 1px solid ${theme.colors.border.weak};
      flex-wrap: wrap;
    `,
    headerTitle: css`
      font-weight: ${theme.typography.fontWeightMedium};
      font-size: ${theme.typography.h5.fontSize};
    `,
    statusOnline: css` color: ${theme.colors.success.text}; font-size: ${theme.typography.bodySmall.fontSize}; `,
    statusOffline: css` color: ${theme.colors.error.text}; font-size: ${theme.typography.bodySmall.fontSize}; `,
    demos: css`
      display: flex;
      flex-wrap: wrap;
      gap: ${theme.spacing(0.5)};
      margin-left: auto;
    `,

    // Stream area
    stream: css`
      flex: 1;
      overflow-y: auto;
      padding: ${theme.spacing(2)};
      display: flex;
      flex-direction: column;
      gap: ${theme.spacing(1.5)};
    `,
    emptyState: css`
      color: ${theme.colors.text.secondary};
      text-align: center;
      padding: ${theme.spacing(8)} 0;
      font-style: italic;
    `,

    // Message types
    userMsg: css`
      align-self: flex-end;
      background: ${theme.colors.primary.main};
      color: ${theme.colors.primary.contrastText};
      padding: ${theme.spacing(1)} ${theme.spacing(2)};
      border-radius: ${theme.shape.radius.default};
      max-width: 70%;
    `,
    agentMsg: css`
      align-self: flex-start;
      background: ${theme.colors.background.secondary};
      padding: ${theme.spacing(1.5)} ${theme.spacing(2)};
      border-radius: ${theme.shape.radius.default};
      max-width: 85%;
      font-size: ${theme.typography.bodySmall.fontSize};
      line-height: 1.6;

      code {
        background: ${theme.colors.background.canvas};
        border: 1px solid ${theme.colors.border.weak};
        border-radius: 3px;
        padding: 0 4px;
        font-size: 11px;
        font-family: ${theme.typography.fontFamilyMonospace};
      }
      pre { margin: ${theme.spacing(0.5)} 0; padding: ${theme.spacing(0.5)} ${theme.spacing(1)};
        background: ${theme.colors.background.canvas}; border: 1px solid ${theme.colors.border.weak};
        border-radius: 3px; overflow-x: auto;
        code { border: none; padding: 0; background: none; }
      }
      .chat-bullet { color: ${theme.colors.primary.text}; font-weight: bold; }

      .inline-diagnosis {
        margin: ${theme.spacing(1)} 0;
        padding: ${theme.spacing(1.5)} ${theme.spacing(2)};
        border-left: 3px solid ${theme.colors.primary.main};
        background: ${theme.colors.background.canvas};
        border-radius: 0 ${theme.shape.radius.default} ${theme.shape.radius.default} 0;
        font-size: 14px;
        line-height: 1.7;

        .dx-section {
          display: inline-block;
          margin-top: ${theme.spacing(1.5)};
          margin-bottom: ${theme.spacing(0.5)};
          font-size: 15px;
          font-weight: ${theme.typography.fontWeightBold};
          color: ${theme.colors.primary.text};
        }
        .dx-bullet { color: ${theme.colors.primary.text}; font-weight: bold; }
      }
    `,
    toolCard: css`
      display: inline-flex;
      align-items: center;
      gap: ${theme.spacing(0.75)};
      background: ${theme.colors.background.canvas};
      border: 1px solid ${theme.colors.border.weak};
      border-radius: ${theme.shape.radius.default};
      padding: ${theme.spacing(0.5)} ${theme.spacing(1)};
      font-size: ${theme.typography.bodySmall.fontSize};
      font-family: ${theme.typography.fontFamilyMonospace};
    `,
    toolIcon: css` font-size: 14px; `,
    toolName: css` color: ${theme.colors.text.primary}; `,
    toolDuration: css` color: ${theme.colors.text.secondary}; `,
    toolCached: css` color: ${theme.colors.success.text}; font-size: 11px; `,

    // Panels
    panelRow: css`
      width: 100%;
    `,

    // System
    systemMsg: css`
      align-self: center;
      color: ${theme.colors.text.secondary};
      font-size: ${theme.typography.bodySmall.fontSize};
      display: flex;
      align-items: center;
      gap: ${theme.spacing(0.5)};
    `,

    // Diagnosis
    diagnosisContainer: css`
      border: 2px solid ${theme.colors.primary.border};
      border-radius: ${theme.shape.radius.default};
      background: ${theme.colors.background.primary};
      overflow: hidden;
      margin-top: ${theme.spacing(1)};
    `,
    diagnosisHeader: css`
      padding: ${theme.spacing(1.5)} ${theme.spacing(2)};
      font-size: ${theme.typography.h4.fontSize};
      font-weight: ${theme.typography.fontWeightBold};
      color: ${theme.colors.text.primary};
      border-bottom: 1px solid ${theme.colors.border.weak};
      background: ${theme.colors.background.secondary};
    `,
    diagnosisBody: css`
      padding: ${theme.spacing(2)} ${theme.spacing(3)};
      font-size: 15px;
      line-height: 1.8;
      color: ${theme.colors.text.primary};
      word-break: break-word;
      .dx-section { display: inline-block; margin-top: ${theme.spacing(2)}; margin-bottom: ${theme.spacing(0.5)};
        font-size: 16px; font-weight: ${theme.typography.fontWeightBold}; color: ${theme.colors.primary.text}; }
      code { background: ${theme.colors.background.canvas}; border: 1px solid ${theme.colors.border.weak};
        border-radius: 3px; padding: 1px 5px; font-size: 13px; font-family: ${theme.typography.fontFamilyMonospace}; }
      .dx-bullet { color: ${theme.colors.primary.text}; font-weight: bold; margin-right: 2px; }
    `,
    // Input
    inputArea: css`
      padding: ${theme.spacing(1)} ${theme.spacing(2)};
      border-top: 1px solid ${theme.colors.border.weak};
      display: flex;
      flex-direction: column;
      gap: ${theme.spacing(0.5)};
    `,
    inputButtons: css`
      display: flex;
      gap: ${theme.spacing(0.5)};
    `,
  };
}
