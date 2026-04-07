import React, { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { css } from '@emotion/css';
import { GrafanaTheme2 } from '@grafana/data';
import { useStyles2 } from '@grafana/ui';
import {
  EmbeddedScene,
  SceneFlexLayout,
  SceneFlexItem,
  SceneQueryRunner,
  VizPanel,
  SceneTimeRange,
} from '@grafana/scenes';

const DS_TYPE_MAP: Record<string, string> = {
  prometheus: 'prometheus',
  loki: 'loki',
  tempo: 'tempo',
};

function toIso(time: string): string {
  if (!time) {
    return '';
  }
  const d = new Date(time);
  return isNaN(d.getTime()) ? time : d.toISOString();
}

/** Ensure from/to are valid; fall back to last 30 minutes. */
function safeTimeRange(from: string, to: string): { from: string; to: string } {
  const f = toIso(from);
  const t = toIso(to);
  if (f && t) {
    return { from: f, to: t };
  }
  return { from: 'now-30m', to: 'now' };
}

function buildQuery(datasourceUid: string, queryStr: string): Record<string, unknown> {
  if (datasourceUid === 'tempo' || datasourceUid.includes('traces')) {
    return { refId: 'A', query: queryStr, queryType: 'traceql' };
  }
  if (datasourceUid === 'loki' || datasourceUid.includes('logs')) {
    return { refId: 'A', expr: queryStr, queryType: 'range', maxLines: 200 };
  }
  return { refId: 'A', expr: queryStr };
}

/** Derive a short Y-axis label and unit from the query string. */
function deriveAxisInfo(query: string): { label: string; unit: string } {
  const q = query.toLowerCase();
  if (q.includes('histogram_quantile') || q.includes('duration') || q.includes('latency')) {
    if (q.includes('milliseconds') || q.includes('_ms')) {
      return { label: 'Duration (ms)', unit: 'ms' };
    }
    return { label: 'Duration (s)', unit: 's' };
  }
  if (q.includes('rate(') || q.includes('_rate')) {
    return { label: 'Rate (/s)', unit: 'ops' };
  }
  if (q.includes('count') || q.includes('calls') || q.includes('total')) {
    return { label: 'Count', unit: 'short' };
  }
  if (q.includes('bytes')) {
    return { label: 'Bytes', unit: 'bytes' };
  }
  if (q.includes('up')) {
    return { label: 'Status', unit: 'short' };
  }
  return { label: 'Value', unit: 'short' };
}

function buildPanelExtras(panelType: string, query: string) {
  if (panelType === 'timeseries') {
    const axis = deriveAxisInfo(query);
    return {
      options: {
        legend: { displayMode: 'list' as const, placement: 'bottom' as const },
        tooltip: { mode: 'multi' as const, sort: 'desc' as const },
      },
      fieldConfig: {
        defaults: {
          custom: { lineWidth: 2, fillOpacity: 10, showPoints: 'auto' as const, axisPlacement: 'auto' as const, axisBorderShow: true, axisLabel: axis.label },
          unit: axis.unit,
        },
        overrides: [],
      },
    };
  }
  if (panelType === 'logs') {
    return {
      options: { showTime: true, showLabels: true, wrapLogMessage: true, enableLogDetails: true, sortOrder: 'Ascending' as const },
    };
  }
  return {};
}

interface Props {
  panelType: string;
  title: string;
  datasourceUid: string;
  query: string;
  timeFrom: string;
  timeTo: string;
}

export function InlinePanel({ panelType, title, datasourceUid, query, timeFrom, timeTo }: Props) {
  const s = useStyles2(getStyles);
  const [expanded, setExpanded] = useState(false);
  const [inlineScene, setInlineScene] = useState<EmbeddedScene | null>(null);
  const [fullScene, setFullScene] = useState<EmbeddedScene | null>(null);

  // Inline scene (fixed height)
  useEffect(() => {
    const safe = safeTimeRange(timeFrom, timeTo);
    const tr = new SceneTimeRange({ from: safe.from, to: safe.to });
    const dsType = DS_TYPE_MAP[datasourceUid] ?? datasourceUid;
    const extras = buildPanelExtras(panelType, query);

    const runner = new SceneQueryRunner({
      datasource: { uid: datasourceUid, type: dsType },
      queries: [buildQuery(datasourceUid, query)],
    });

    const item = new SceneFlexItem({
      height: 280,
      body: new VizPanel({ title, pluginId: panelType, $data: runner, ...extras }),
      $timeRange: tr,
    });

    const layout = new SceneFlexLayout({ direction: 'column', children: [item] });
    setInlineScene(new EmbeddedScene({ $timeRange: tr, body: layout }));
  }, [panelType, title, datasourceUid, query, timeFrom, timeTo]);

  // Fullscreen scene (created on demand, fills viewport)
  useEffect(() => {
    if (!expanded) {
      setFullScene(null);
      return;
    }

    const safe = safeTimeRange(timeFrom, timeTo);
    const tr = new SceneTimeRange({ from: safe.from, to: safe.to });
    const dsType = DS_TYPE_MAP[datasourceUid] ?? datasourceUid;
    const extras = buildPanelExtras(panelType, query);

    const runner = new SceneQueryRunner({
      datasource: { uid: datasourceUid, type: dsType },
      queries: [buildQuery(datasourceUid, query)],
    });

    const item = new SceneFlexItem({
      body: new VizPanel({ title, pluginId: panelType, $data: runner, ...extras }),
      $timeRange: tr,
    });

    const layout = new SceneFlexLayout({ direction: 'column', children: [item] });
    setFullScene(new EmbeddedScene({ $timeRange: tr, body: layout }));
  }, [expanded, panelType, title, datasourceUid, query, timeFrom, timeTo]);

  if (!inlineScene) {
    return null;
  }

  return (
    <>
      <div className={s.wrapper}>
        <button
          className={s.expandBtn}
          onClick={() => setExpanded(true)}
          title="Expand panel"
        >
          &#x26F6;
        </button>
        <inlineScene.Component model={inlineScene} />
      </div>

      {expanded && fullScene && createPortal(
        <div className={s.overlay}>
          <div className={s.overlayHeader}>
            <span className={s.overlayTitle}>{title}</span>
            <button className={s.closeBtn} onClick={() => setExpanded(false)} title="Close">
              &#x2715;
            </button>
          </div>
          <div className={s.overlayBody}>
            <fullScene.Component model={fullScene} />
          </div>
        </div>,
        document.body
      )}
    </>
  );
}

function getStyles(theme: GrafanaTheme2) {
  return {
    wrapper: css`
      margin: ${theme.spacing(1)} 0;
      border: 1px solid ${theme.colors.border.weak};
      border-radius: ${theme.shape.radius.default};
      overflow: hidden;
      position: relative;

      &:hover button {
        opacity: 1;
      }
    `,
    expandBtn: css`
      position: absolute;
      top: ${theme.spacing(0.5)};
      right: ${theme.spacing(0.5)};
      z-index: 10;
      opacity: 0;
      transition: opacity 0.15s;
      background: ${theme.colors.background.primary};
      border: 1px solid ${theme.colors.border.medium};
      border-radius: ${theme.shape.radius.default};
      color: ${theme.colors.text.secondary};
      cursor: pointer;
      padding: 4px 8px;
      font-size: 16px;
      line-height: 1;

      &:hover {
        color: ${theme.colors.text.primary};
        background: ${theme.colors.background.secondary};
      }
    `,

    // Fullscreen overlay
    overlay: css`
      position: fixed;
      inset: 0;
      z-index: 10000;
      background: ${theme.colors.background.primary};
      display: flex;
      flex-direction: column;
    `,
    overlayHeader: css`
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: ${theme.spacing(1.5)} ${theme.spacing(2)};
      border-bottom: 1px solid ${theme.colors.border.weak};
      background: ${theme.colors.background.secondary};
      flex-shrink: 0;
    `,
    overlayTitle: css`
      font-size: ${theme.typography.h4.fontSize};
      font-weight: ${theme.typography.fontWeightMedium};
      color: ${theme.colors.text.primary};
    `,
    closeBtn: css`
      background: none;
      border: 1px solid ${theme.colors.border.medium};
      border-radius: ${theme.shape.radius.default};
      color: ${theme.colors.text.secondary};
      cursor: pointer;
      padding: 6px 12px;
      font-size: 18px;
      line-height: 1;

      &:hover {
        color: ${theme.colors.text.primary};
        background: ${theme.colors.background.canvas};
      }
    `,
    overlayBody: css`
      flex: 1;
      overflow: auto;
      padding: ${theme.spacing(2)};
    `,
  };
}
