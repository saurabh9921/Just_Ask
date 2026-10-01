import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';
import { MatButtonModule } from '@angular/material/button';

import type {
  Analysis,
  KpiCard,
  QueryResponse,
  ResultColumn,
} from '../../core/messaging/analytics-types';
import { KpiCardComponent } from '../../dashboards/kpi-card.component';
import { ResultChartComponent } from '../../dashboards/result-chart.component';
import { ResultTableComponent } from '../../dashboards/result-table.component';
import {
  buildAnalysisChartModel,
  buildChartModel,
  type ChartModel,
} from '../../dashboards/chart-options';
import {
  downloadFile,
  exportFileName,
  responseToCsv,
  responseToJson,
} from '../../shared/export/export';
import { IconComponent } from '../../shared/icon/icon.component';
import { BackgroundExportComponent } from '../background-export/background-export.component';
import { type AnswerSummary, summarizeAnswer } from './answer-summary';
import { QueryPlanViewerComponent } from '../query-plan-viewer/query-plan-viewer.component';
import { SourceMetadataComponent } from '../source-metadata/source-metadata.component';

type ExportFormat = 'csv' | 'json';

/** How an answer is shown: one visual next to the summary. */
type Visual = 'chart' | 'kpis' | 'table';

const MEASURE_KINDS: ReadonlySet<ResultColumn['kind']> = new Set([
  'measure',
  'previous',
  'change',
  'change_pct',
]);

/** Headline figures of a business analysis shown when no chart passes validation. */
const MAX_TILES = 4;
/** A summary stays short: more points and notes are behind "Show all". */
const MAX_POINTS = 6;
const MAX_NOTES = 4;

/**
 * A validated answer, shared by the chat and the history view, presented like a chat
 * reply: one visual - the chart, KPI cards for single-figure answers, or a plain
 * table for lists - followed by a short plain-language summary (`summarizeAnswer`),
 * with no tabs. The full response (rows, plan, provenance) is unchanged and still
 * exported; "Plan only" answers, which have no rows, show their plan.
 *
 * A chart is only drawn when the server enabled and validated it and the client's own
 * check passes (`buildChartModel`); otherwise the KPIs or the table are shown. A
 * business analysis ("lead analysis for …") shows its validated chart - or its
 * headline figures when there is none - then its title, statements and missing-data
 * notes.
 */
@Component({
  selector: 'eia-result-view',
  imports: [
    MatButtonModule,
    BackgroundExportComponent,
    IconComponent,
    KpiCardComponent,
    ResultChartComponent,
    ResultTableComponent,
    QueryPlanViewerComponent,
    SourceMetadataComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @let r = response();
    @if (r.result; as result) {
      @switch (visual()) {
        @case ('chart') {
          <div class="visual" data-testid="chart-answer">
            <eia-result-chart [model]="chartModel()!" />
          </div>
        }
        @case ('kpis') {
          <div class="kpis" data-testid="kpi-answer">
            @for (kpi of kpis(); track kpi.name) {
              <eia-kpi-card [kpi]="kpi" />
            }
          </div>
        }
        @default {
          @if (result.kpis.length > 0) {
            <div class="kpis">
              @for (kpi of result.kpis; track kpi.name) {
                <eia-kpi-card [kpi]="kpi" />
              }
            </div>
          }
          <div class="visual" data-testid="table-answer">
            <eia-result-table [result]="result" />
          </div>
        }
      }

      @if (takeaway(); as t) {
        <section class="takeaway" aria-label="Summary" data-testid="answer-summary">
          <p class="takeaway__title">Summary</p>
          @if (t.title) {
            <p class="takeaway__heading" data-testid="answer-title">{{ t.title }}</p>
          }
          @if (t.points.length > 0) {
            <ul class="takeaway__points">
              @for (point of shown(t.points, maxPoints); track $index) {
                <li>{{ point }}</li>
              }
            </ul>
          } @else {
            <p class="takeaway__text">{{ t.text }}</p>
          }
          @if (t.notes.length > 0) {
            <div class="takeaway__notes" role="note" data-testid="answer-notes">
              <p class="takeaway__subtitle">Notes on the data</p>
              <ul>
                @for (note of shown(t.notes, maxNotes); track $index) {
                  <li>{{ note }}</li>
                }
              </ul>
            </div>
          }
          @if (hidden(t) > 0) {
            <button
              mat-button
              type="button"
              class="takeaway__more"
              data-testid="show-all"
              (click)="showAll.set(true)"
            >
              Show all ({{ hidden(t) }} more)
            </button>
          }
          @if (t.period) {
            <p class="takeaway__period">Period: {{ t.period }}</p>
          }
        </section>
      }

      @if (canExport()) {
        <div class="export" role="group" aria-label="Export">
          @if (pending(); as format) {
            <span class="export__confirm" role="alert">
              Export {{ result.rows.length }} rows of detail data as {{ format.toUpperCase() }}? The
              file is saved to this computer.
            </span>
            <button mat-flat-button type="button" (click)="confirmExport()">Export</button>
            <button mat-button type="button" (click)="pending.set(null)">Cancel</button>
          } @else {
            <button mat-button type="button" (click)="requestExport('csv')">
              <eia-icon name="download" [size]="16" /> CSV
            </button>
            <button mat-button type="button" (click)="requestExport('json')">
              <eia-icon name="download" [size]="16" /> JSON
            </button>
          }
        </div>
        @if (pending() === null && result.rows.length > 1) {
          <eia-background-export [queryId]="r.queryId" />
        }
      }
    } @else {
      @if (r.explanation; as explanation) {
        <p class="explanation">{{ explanation.summary }}</p>
      }
      <eia-query-plan-viewer [response]="r" />
      <div class="meta"><eia-source-metadata [response]="r" /></div>
    }
  `,
  styles: `
    .takeaway {
      margin-top: 10px;
      padding: 10px 12px;
      border-radius: 10px;
      border-left: 3px solid var(--mat-sys-primary);
      background: var(--mat-sys-surface-container-low);
    }
    .takeaway__title {
      margin: 0 0 2px;
      font: var(--mat-sys-label-large);
      color: var(--mat-sys-primary);
    }
    .takeaway__text {
      margin: 0;
      font: var(--mat-sys-body-medium);
      line-height: 1.5;
    }
    .takeaway__heading {
      margin: 0 0 4px;
      font: var(--mat-sys-title-small);
    }
    .takeaway__points {
      margin: 0;
      padding-left: 18px;
      font: var(--mat-sys-body-medium);
      line-height: 1.5;
    }
    .takeaway__notes {
      margin-top: 8px;
      font: var(--mat-sys-body-small);
      color: var(--mat-sys-on-surface-variant);
    }
    .takeaway__notes ul {
      margin: 0;
      padding-left: 18px;
    }
    .takeaway__subtitle {
      margin: 0 0 2px;
      font: var(--mat-sys-label-medium);
    }
    .takeaway__more {
      margin-top: 4px;
    }
    .takeaway__period {
      margin: 6px 0 0;
      font: var(--mat-sys-label-medium);
      color: var(--mat-sys-on-surface-variant);
    }
    .explanation {
      margin: 0 0 10px;
      font: var(--mat-sys-body-large);
    }
    .kpis {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
      gap: 8px;
      margin-bottom: 8px;
    }
    .visual {
      margin-top: 4px;
    }
    .meta {
      margin-top: 10px;
    }
    .export {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 4px;
      margin-top: 6px;
    }
    .export__confirm {
      font: var(--mat-sys-body-small);
      flex-basis: 100%;
    }
  `,
})
export class ResultViewComponent {
  readonly response = input.required<QueryResponse>();
  readonly canExport = input(false);

  protected readonly pending = signal<ExportFormat | null>(null);
  protected readonly showAll = signal(false);
  protected readonly maxPoints = MAX_POINTS;
  protected readonly maxNotes = MAX_NOTES;

  /** The plain-language summary shown after the visual. */
  protected readonly takeaway = computed(() => summarizeAnswer(this.response()));

  /** A business analysis ("Lead Analysis — …"), which brings its own chart and figures. */
  private readonly report = computed<Analysis | null>(() => {
    const analysis = this.response().analysis;
    return analysis !== null && analysis.title !== null ? analysis : null;
  });

  /** Only a chart that passed the server's and the client's validation is drawn. */
  protected readonly chartModel = computed<ChartModel | null>(() => {
    const report = this.report();
    if (report !== null) {
      return buildAnalysisChartModel(report.chart);
    }
    const result = this.response().result;
    return result === null ? null : buildChartModel(result);
  });

  /** KPI cards: the result's, or an analysis's headline figures (counts, rates, values). */
  protected readonly kpis = computed<readonly KpiCard[]>(() => {
    const report = this.report();
    if (report === null) {
      return this.response().result?.kpis ?? [];
    }
    return report.metrics
      .filter((m) => m.validated && m.value !== null && !/(^|_)by_/.test(m.key))
      .slice(0, MAX_TILES)
      .map((m) => ({
        name: m.key,
        label: m.label,
        value: m.value,
        format: m.format,
        previous: null,
        change: null,
        changePct: null,
        scope: 'value' as const,
      }));
  });

  protected readonly visual = computed<Visual>(() => {
    const result = this.response().result;
    if (this.chartModel() !== null) return 'chart';
    if (this.report() !== null) return 'kpis';
    if (result === null) return 'table';
    const figuresOnly = result.columns.every((c) => MEASURE_KINDS.has(c.kind));
    // The server rejected every breakdown (blank, free text): the figures are the answer.
    const rejected = !result.chart.enabled && result.chart.type === 'kpi';
    if (result.kpis.length > 0 && (rejected || (result.rows.length <= 1 && figuresOnly))) {
      return 'kpis';
    }
    return 'table';
  });

  /** The first `max` items, or all of them after "Show all". */
  protected shown(items: readonly string[], max: number): readonly string[] {
    return this.showAll() ? items : items.slice(0, max);
  }

  /** How many points and notes "Show all" would add. */
  protected hidden(t: AnswerSummary): number {
    if (this.showAll()) {
      return 0;
    }
    return Math.max(0, t.points.length - MAX_POINTS) + Math.max(0, t.notes.length - MAX_NOTES);
  }

  /** Single-value answers export directly; detailed rows need an explicit confirmation. */
  protected requestExport(format: ExportFormat): void {
    const rows = this.response().result?.rows.length ?? 0;
    if (rows > 1) {
      this.pending.set(format);
    } else {
      this.export(format);
    }
  }

  protected confirmExport(): void {
    const format = this.pending();
    this.pending.set(null);
    if (format !== null) {
      this.export(format);
    }
  }

  private export(format: ExportFormat): void {
    const response = this.response();
    if (format === 'csv') {
      downloadFile(
        exportFileName(response.question, 'csv'),
        'text/csv;charset=utf-8',
        responseToCsv(response),
      );
    } else {
      downloadFile(
        exportFileName(response.question, 'json'),
        'application/json',
        responseToJson(response),
      );
    }
  }
}
