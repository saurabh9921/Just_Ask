import { Injectable, InjectionToken, inject } from '@angular/core';
import { Observable, Subject } from 'rxjs';

import {
  ANALYTICS_PORT,
  type AskRequest,
  type StreamCommand,
  type StreamEvent,
} from './analytics-types';

/** The subset of `chrome.runtime.Port` used by the side panel. */
export interface RuntimePort {
  postMessage(message: StreamCommand): void;
  disconnect(): void;
  readonly onMessage: { addListener(listener: (message: unknown) => void): void };
  readonly onDisconnect: { addListener(listener: () => void): void };
}

export type PortConnector = (name: string) => RuntimePort;

function detectConnector(): PortConnector | null {
  const runtime = (globalThis as { chrome?: typeof chrome }).chrome?.runtime;
  if (runtime?.id === undefined || typeof runtime.connect !== 'function') {
    return null;
  }
  return (name) => runtime.connect({ name });
}

/** `null` outside the extension. */
export const PORT_CONNECTOR = new InjectionToken<PortConnector | null>('PORT_CONNECTOR', {
  providedIn: 'root',
  factory: detectConnector,
});

const TERMINAL = new Set(['result', 'error']);

function newRef(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(9));
  return `q_${Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')}`;
}

function isStreamEvent(value: unknown): value is StreamEvent {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as { kind?: unknown }).kind === 'string' &&
    typeof (value as { ref?: unknown }).ref === 'string'
  );
}

export interface StreamHandle {
  readonly ref: string;
  readonly events: Observable<StreamEvent>;
}

/**
 * Streamed questions via the background worker (which owns the WebSocket and the
 * HTTP fallback). One port carries all questions of this panel, demultiplexed by
 * `ref`. If the background worker restarts, the port closes and every running
 * question ends with a `NETWORK_ERROR` the UI can retry.
 */
@Injectable({ providedIn: 'root' })
export class AnalyticsStreamService {
  private readonly connector = inject(PORT_CONNECTOR);
  private port: RuntimePort | null = null;
  private readonly streams = new Map<string, Subject<StreamEvent>>();

  get available(): boolean {
    return this.connector !== null;
  }

  ask(request: AskRequest): StreamHandle {
    const ref = newRef();
    const subject = new Subject<StreamEvent>();
    this.streams.set(ref, subject);
    const events = new Observable<StreamEvent>((subscriber) => {
      const subscription = subject.subscribe(subscriber);
      queueMicrotask(() => this.send({ kind: 'ask', ref, request }));
      return () => subscription.unsubscribe();
    });
    return { ref, events };
  }

  cancel(ref: string): void {
    if (this.streams.has(ref)) {
      this.send({ kind: 'cancel', ref });
    }
  }

  private send(command: StreamCommand): void {
    const port = this.ensurePort();
    if (port === null) {
      this.emit({
        kind: 'error',
        ref: command.ref,
        error: {
          code: 'NOT_IN_EXTENSION',
          message: 'ERP Insight AI is not running inside the browser extension.',
          status: 0,
        },
        response: null,
      });
      return;
    }
    try {
      port.postMessage(command);
    } catch {
      this.dropPort();
      this.emit({
        kind: 'error',
        ref: command.ref,
        error: {
          code: 'NETWORK_ERROR',
          message: 'The extension background service is not available. Try again.',
          status: 0,
        },
        response: null,
      });
    }
  }

  private ensurePort(): RuntimePort | null {
    if (this.port !== null) {
      return this.port;
    }
    if (this.connector === null) {
      return null;
    }
    const port = this.connector(ANALYTICS_PORT);
    port.onMessage.addListener((message) => {
      if (isStreamEvent(message)) {
        this.emit(message);
      }
    });
    port.onDisconnect.addListener(() => {
      if (this.port === port) {
        this.dropPort();
      }
    });
    this.port = port;
    return port;
  }

  private dropPort(): void {
    this.port = null;
    for (const ref of [...this.streams.keys()]) {
      this.emit({
        kind: 'error',
        ref,
        error: {
          code: 'NETWORK_ERROR',
          message: 'The connection to the extension background service was lost. Try again.',
          status: 0,
        },
        response: null,
      });
    }
  }

  private emit(event: StreamEvent): void {
    const subject = this.streams.get(event.ref);
    if (subject === undefined) {
      return;
    }
    subject.next(event);
    if (TERMINAL.has(event.kind)) {
      this.streams.delete(event.ref);
      subject.complete();
    }
  }
}
