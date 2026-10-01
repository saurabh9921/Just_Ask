import { Injectable, InjectionToken, inject } from '@angular/core';
import { Observable, defer, map, throwError, timeout } from 'rxjs';

import { environment } from '../../../environments/environment';
import {
  isExtensionResponse,
  type ExtensionRequestOf,
  type MessageType,
  type ProtocolErrorCode,
  type ResponsePayloads,
} from './protocol';

/** The subset of `chrome.runtime` the bridge needs; injectable for tests. */
export interface RuntimeMessenger {
  readonly id: string;
  sendMessage(message: unknown): Promise<unknown>;
}

function detectRuntime(): RuntimeMessenger | null {
  const runtime = (globalThis as { chrome?: typeof chrome }).chrome?.runtime;
  if (runtime?.id === undefined || typeof runtime.sendMessage !== 'function') {
    return null;
  }
  return {
    id: runtime.id,
    sendMessage: (message: unknown) => runtime.sendMessage(message),
  };
}

/** Listener registration for events pushed by the background worker. */
export interface RuntimeEvents {
  addListener(listener: (message: unknown, sender: { id?: string | undefined }) => void): void;
  removeListener(listener: (message: unknown, sender: { id?: string | undefined }) => void): void;
}

function detectRuntimeEvents(): RuntimeEvents | null {
  const onMessage = (
    globalThis as { chrome?: { runtime?: { onMessage?: typeof chrome.runtime.onMessage } } }
  ).chrome?.runtime?.onMessage;
  if (onMessage === undefined) {
    return null;
  }
  return {
    addListener: (listener) => {
      onMessage.addListener(listener);
    },
    removeListener: (listener) => {
      onMessage.removeListener(listener);
    },
  };
}

/** `null` outside the extension. */
export const RUNTIME_EVENTS = new InjectionToken<RuntimeEvents | null>('RUNTIME_EVENTS', {
  providedIn: 'root',
  factory: detectRuntimeEvents,
});

/** `null` when the app runs outside the extension (e.g. `ng serve`). */
export const RUNTIME_MESSENGER = new InjectionToken<RuntimeMessenger | null>('RUNTIME_MESSENGER', {
  providedIn: 'root',
  factory: detectRuntime,
});

export const BRIDGE_TIMEOUT_MS = new InjectionToken<number>('BRIDGE_TIMEOUT_MS', {
  providedIn: 'root',
  factory: () => environment.bridgeTimeoutMs,
});

export class BridgeError extends Error {
  override readonly name = 'BridgeError';

  constructor(
    readonly code: ProtocolErrorCode,
    message: string,
    readonly retryAfterSeconds: number | null = null,
    readonly requestId: string | null = null,
  ) {
    super(message);
  }
}

/**
 * Typed request/response channel from the side panel to the background worker.
 *
 * The side panel has no network or token access of its own; every backend call goes
 * through this bridge.
 */
@Injectable({ providedIn: 'root' })
export class BackgroundBridgeService {
  private readonly runtime = inject(RUNTIME_MESSENGER);
  private readonly timeoutMs = inject(BRIDGE_TIMEOUT_MS);

  get inExtension(): boolean {
    return this.runtime !== null;
  }

  request<K extends MessageType>(message: ExtensionRequestOf<K>): Observable<ResponsePayloads[K]> {
    const runtime = this.runtime;
    if (runtime === null) {
      return throwError(
        () =>
          new BridgeError(
            'NOT_IN_EXTENSION',
            'ERP Insight AI is not running inside the browser extension.',
          ),
      );
    }
    return defer(() => runtime.sendMessage(message)).pipe(
      timeout({
        first: this.timeoutMs,
        with: () =>
          throwError(
            () => new BridgeError('TIMEOUT', 'The extension background service did not respond.'),
          ),
      }),
      map((response) => {
        if (!isExtensionResponse(response)) {
          throw new BridgeError(
            'INVALID_MESSAGE',
            'Received an invalid response from the background service.',
          );
        }
        if (!response.ok) {
          throw new BridgeError(
            response.error.code,
            response.error.message,
            response.error.retryAfterSeconds ?? null,
            response.error.requestId ?? null,
          );
        }
        return response.data as ResponsePayloads[K];
      }),
    );
  }
}

/** Converts any error into a user-safe message. */
export function describeBridgeError(error: unknown): string {
  if (error instanceof BridgeError) {
    return error.message;
  }
  return 'An unexpected error occurred.';
}
