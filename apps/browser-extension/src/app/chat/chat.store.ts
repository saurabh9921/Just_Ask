import { Injectable, computed, inject, signal } from '@angular/core';
import { Observable, tap } from 'rxjs';

import { AuthService } from '../core/auth/auth.service';
import { AnalyticsStreamService } from '../core/messaging/analytics-stream.service';
import type {
  AskRequest,
  FeedbackInput,
  FeedbackResult,
  ProgressEvent,
  QueryMode,
  QueryResponse,
  StreamError,
  StreamEvent,
} from '../core/messaging/analytics-types';
import { BackgroundBridgeService, BridgeError } from '../core/messaging/background-bridge.service';

export type TurnPhase = 'running' | 'done' | 'failed';

export interface StepProgress {
  readonly step: string;
  readonly message: string;
  readonly status: ProgressEvent['status'];
}

export interface ChatTurn {
  readonly id: string;
  readonly question: string;
  readonly mode: QueryMode;
  readonly connectionId: string | null;
  readonly parentQueryId: string | null;
  readonly parentQuestion: string | null;
  readonly phase: TurnPhase;
  readonly queryId: string | null;
  readonly steps: readonly StepProgress[];
  readonly elapsedMs: number;
  readonly transport: 'websocket' | 'http' | null;
  readonly response: QueryResponse | null;
  readonly error: StreamError | null;
  readonly cancelling: boolean;
  readonly saved: boolean;
  readonly feedback: -1 | 1 | null;
}

export interface AskOptions {
  readonly mode?: QueryMode;
  readonly connectionId: string | null;
  readonly followUpOf?: ChatTurn | null;
}

const MAX_TURNS = 50;
const SESSION_ERRORS = new Set(['SESSION_EXPIRED', 'UNAUTHENTICATED', 'TOKEN_EXPIRED']);

/**
 * The conversation in the side panel: questions, streamed progress and answers.
 *
 * Kept in memory for the panel's lifetime (answers can contain business data, so
 * they are never written to extension storage); the server-side history lists
 * past chats and keeps answers for a short time. Every question carries this
 * chat's conversation id so the history groups the chat as one entry; "New chat"
 * (`clear`) starts a new conversation.
 */
@Injectable({ providedIn: 'root' })
export class ChatStore {
  private readonly stream = inject(AnalyticsStreamService);
  private readonly bridge = inject(BackgroundBridgeService);
  private readonly auth = inject(AuthService);

  private readonly _turns = signal<readonly ChatTurn[]>([]);
  readonly turns = this._turns.asReadonly();
  private conversationId = crypto.randomUUID();
  readonly running = computed(() => this._turns().some((t) => t.phase === 'running'));
  readonly canExport = computed(
    () => this.auth.user()?.permissions.includes('analytics.export') ?? false,
  );

  ask(question: string, options: AskOptions): ChatTurn | null {
    const text = question.trim();
    const parent = options.followUpOf ?? null;
    if (text === '' || (options.connectionId === null && parent === null)) {
      return null;
    }
    const request: AskRequest = {
      question: text,
      connectionId: options.connectionId,
      parentQueryId: parent?.queryId ?? null,
      conversationId: this.conversationId,
      mode: options.mode ?? 'execute',
    };
    const handle = this.stream.ask(request);
    const turn: ChatTurn = {
      id: handle.ref,
      question: text,
      mode: request.mode,
      connectionId: request.connectionId,
      parentQueryId: request.parentQueryId,
      parentQuestion: parent?.question ?? null,
      phase: 'running',
      queryId: null,
      steps: [],
      elapsedMs: 0,
      transport: null,
      response: null,
      error: null,
      cancelling: false,
      saved: false,
      feedback: null,
    };
    this._turns.update((turns) => [...turns, turn].slice(-MAX_TURNS));
    handle.events.subscribe((event) => this.apply(turn.id, event));
    return turn;
  }

  retry(turn: ChatTurn): ChatTurn | null {
    const parent =
      turn.parentQueryId === null
        ? null
        : (this._turns().find((t) => t.queryId === turn.parentQueryId) ?? null);
    return this.ask(turn.question, {
      mode: turn.mode,
      connectionId: turn.connectionId,
      followUpOf: parent,
    });
  }

  cancel(turn: ChatTurn): void {
    if (turn.phase !== 'running') {
      return;
    }
    this.patch(turn.id, { cancelling: true });
    this.stream.cancel(turn.id);
  }

  /** Starts a new chat; questions still running stay visible. */
  clear(): void {
    this._turns.set(this._turns().filter((t) => t.phase === 'running'));
    this.conversationId = crypto.randomUUID();
  }

  setSaved(turn: ChatTurn, saved: boolean): Observable<unknown> {
    const queryId = turn.response?.queryId ?? turn.queryId;
    if (queryId === null) {
      throw new Error('The question has not been recorded yet.');
    }
    return this.bridge
      .request<'history.save'>({ type: 'history.save', payload: { queryId, saved } })
      .pipe(tap(() => this.patch(turn.id, { saved })));
  }

  submitFeedback(turn: ChatTurn, input: FeedbackInput): Observable<FeedbackResult> {
    return this.bridge
      .request<'feedback.submit'>({ type: 'feedback.submit', payload: input })
      .pipe(tap((result) => this.patch(turn.id, { feedback: result.rating })));
  }

  /** Test/diagnostic helper: the current state of one turn. */
  get(id: string): ChatTurn | undefined {
    return this._turns().find((t) => t.id === id);
  }

  private apply(id: string, event: StreamEvent): void {
    switch (event.kind) {
      case 'transport':
        this.patch(id, { transport: event.transport });
        return;
      case 'accepted':
        this.patch(id, { queryId: event.queryId });
        return;
      case 'progress':
        this.update(id, (turn) => ({
          ...turn,
          queryId: turn.queryId ?? event.event.queryId,
          elapsedMs: event.event.elapsedMs,
          steps: mergeStep(turn.steps, event.event),
        }));
        return;
      case 'result':
        this.patch(id, {
          phase: 'done',
          response: event.response,
          queryId: event.response.queryId,
          cancelling: false,
        });
        return;
      case 'error':
        if (SESSION_ERRORS.has(event.error.code)) {
          this.auth.sessionEnded();
        }
        this.patch(id, {
          phase: 'failed',
          error: event.error,
          response: event.response,
          queryId: event.response?.queryId ?? this.get(id)?.queryId ?? null,
          cancelling: false,
        });
        return;
    }
  }

  private patch(id: string, changes: Partial<ChatTurn>): void {
    this.update(id, (turn) => ({ ...turn, ...changes }));
  }

  private update(id: string, fn: (turn: ChatTurn) => ChatTurn): void {
    this._turns.update((turns) => turns.map((t) => (t.id === id ? fn(t) : t)));
  }
}

function mergeStep(steps: readonly StepProgress[], event: ProgressEvent): readonly StepProgress[] {
  const next: StepProgress = { step: event.step, message: event.message, status: event.status };
  const index = steps.findIndex((s) => s.step === event.step);
  if (index === -1) {
    return [...steps, next];
  }
  return steps.map((s, i) => (i === index ? next : s));
}

export function isBridgeError(error: unknown): error is BridgeError {
  return error instanceof BridgeError;
}
