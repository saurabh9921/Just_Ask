import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  afterRenderEffect,
  inject,
  input,
  viewChild,
} from '@angular/core';
import { BarChart, LineChart, PieChart, ScatterChart } from 'echarts/charts';
import {
  AriaComponent,
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from 'echarts/components';
import * as echarts from 'echarts/core';
import { SVGRenderer } from 'echarts/renderers';

import type { ChartModel } from './chart-options';

// Tree-shaken ECharts: only the pieces the side panel uses. SVG keeps text crisp in
// a narrow panel and avoids canvas fingerprinting surfaces.
echarts.use([
  BarChart,
  LineChart,
  PieChart,
  ScatterChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  AriaComponent,
  SVGRenderer,
]);

/**
 * Draws a chart model that has already passed validation (`buildChartModel` /
 * `buildAnalysisChartModel` return `null` for charts that must not be shown).
 */
@Component({
  selector: 'eia-result-chart',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @let m = model();
    <figure class="figure">
      @if (m.title) {
        <p class="title">{{ m.title }}</p>
      }
      <div #host class="chart" role="img" [attr.aria-label]="m.description"></div>
      <figcaption class="caption">
        {{ m.description }}
        @if (m.truncatedCategories) {
          See the table for all rows.
        }
      </figcaption>
    </figure>
  `,
  styles: `
    .figure {
      margin: 0;
    }
    .title {
      margin: 0 0 4px;
      font: var(--mat-sys-title-small);
    }
    .chart {
      width: 100%;
      height: 260px;
    }
    .caption {
      margin: 4px 0 0;
      font: var(--mat-sys-body-small);
      color: var(--mat-sys-on-surface-variant);
    }
  `,
})
export class ResultChartComponent {
  readonly model = input.required<ChartModel>();
  private readonly host = viewChild<ElementRef<HTMLDivElement>>('host');
  private chart: echarts.ECharts | null = null;
  private observer: ResizeObserver | null = null;

  constructor() {
    afterRenderEffect(() => {
      const element = this.host()?.nativeElement;
      const model = this.model();
      if (element === undefined) {
        this.dispose();
        return;
      }
      if (this.chart?.getDom() !== element) {
        this.dispose();
        this.chart = echarts.init(element, null, { renderer: 'svg' });
        if (typeof ResizeObserver !== 'undefined') {
          this.observer = new ResizeObserver(() => this.chart?.resize());
          this.observer.observe(element);
        }
      }
      const style = getComputedStyle(element);
      const text = style.getPropertyValue('color') || undefined;
      this.chart.setOption(
        {
          ...model.option,
          textStyle: { color: text, fontFamily: style.getPropertyValue('font-family') },
        } as echarts.EChartsCoreOption,
        { notMerge: true },
      );
    });
    inject(DestroyRef).onDestroy(() => this.dispose());
  }

  private dispose(): void {
    this.observer?.disconnect();
    this.observer = null;
    this.chart?.dispose();
    this.chart = null;
  }
}
