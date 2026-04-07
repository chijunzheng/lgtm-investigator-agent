/**
 * Grafana Scenes helpers for dynamically building an investigation dashboard.
 *
 * The agent's tool queries (PromQL, LogQL, TraceQL) become real Grafana panel
 * queries — Grafana fetches data from the datasources and renders natively.
 */
import {
  EmbeddedScene,
  SceneFlexLayout,
  SceneFlexItem,
  SceneQueryRunner,
  VizPanel,
  SceneTimeRange,
} from '@grafana/scenes';

export interface InvestigationScene {
  scene: EmbeddedScene;
  layout: SceneFlexLayout;
  timeRange: SceneTimeRange;
}

/** Datasource type lookup for UID-based resolution. */
const DS_TYPE_MAP: Record<string, string> = {
  prometheus: 'prometheus',
  loki: 'loki',
  tempo: 'tempo',
};

/** Query format for each datasource type. */
function buildQuery(datasourceUid: string, queryStr: string): Record<string, unknown> {
  if (datasourceUid === 'tempo' || datasourceUid.includes('traces')) {
    return { refId: 'A', query: queryStr, queryType: 'traceql' };
  }
  if (datasourceUid === 'loki' || datasourceUid.includes('logs')) {
    return { refId: 'A', expr: queryStr, queryType: 'range', maxLines: 200 };
  }
  // Prometheus
  return { refId: 'A', expr: queryStr };
}

/** Panel options per visualization type. */
function buildPanelOptions(panelType: string, title: string): { options?: Record<string, unknown>; fieldConfig?: Record<string, unknown> } {
  if (panelType === 'timeseries') {
    return {
      options: {
        legend: { displayMode: 'list', placement: 'bottom' },
        tooltip: { mode: 'multi', sort: 'desc' },
      },
      fieldConfig: {
        defaults: {
          custom: {
            lineWidth: 2,
            fillOpacity: 10,
            showPoints: 'auto',
            axisPlacement: 'auto',
            axisBorderShow: true,
            axisLabel: title,
          },
          unit: 'short',
        },
        overrides: [],
      },
    };
  }
  if (panelType === 'logs') {
    return {
      options: {
        showTime: true,
        showLabels: true,
        showCommonLabels: false,
        wrapLogMessage: true,
        prettifyLogMessage: false,
        enableLogDetails: true,
        sortOrder: 'Ascending',
      },
    };
  }
  return {};
}

/** Ensure time string is valid ISO 8601 for SceneTimeRange (dateMath.parse expects ISO, not epoch ms). */
function toIso(time: string): string {
  const d = new Date(time);
  return isNaN(d.getTime()) ? time : d.toISOString();
}

/** Create an empty investigation scene with a fixed time range. */
export function createInvestigationScene(timeFrom: string, timeTo: string): InvestigationScene {
  const timeRange = new SceneTimeRange({ from: toIso(timeFrom), to: toIso(timeTo) });

  const layout = new SceneFlexLayout({
    direction: 'column',
    children: [],
  });

  const scene = new EmbeddedScene({
    $timeRange: timeRange,
    body: layout,
  });

  return { scene, layout, timeRange };
}

/** Add a visualization panel when the agent discovers evidence. */
export function addPanel(
  layout: SceneFlexLayout,
  panelType: string,
  title: string,
  datasourceUid: string,
  queryStr: string,
  timeRange: SceneTimeRange
): void {
  const dsType = DS_TYPE_MAP[datasourceUid] ?? datasourceUid;
  const { options, fieldConfig } = buildPanelOptions(panelType, title);

  const queryRunner = new SceneQueryRunner({
    datasource: { uid: datasourceUid, type: dsType },
    queries: [buildQuery(datasourceUid, queryStr)],
  });

  const panel = new SceneFlexItem({
    height: 250,
    body: new VizPanel({
      title,
      pluginId: panelType,
      $data: queryRunner,
      ...(options ? { options } : {}),
      ...(fieldConfig ? { fieldConfig } : {}),
    }),
    $timeRange: timeRange,
  });

  layout.setState({
    children: [...layout.state.children, panel],
  });
}

/** Reset the scene layout (clear all panels). */
export function clearPanels(layout: SceneFlexLayout): void {
  layout.setState({ children: [] });
}
