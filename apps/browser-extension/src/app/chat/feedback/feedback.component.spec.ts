import { TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';

import type { FeedbackInput } from '../../core/messaging/analytics-types';
import { BridgeError } from '../../core/messaging/background-bridge.service';
import { QUERY_ID } from '../../../testing/analytics-fixtures';
import { FeedbackComponent } from './feedback.component';

function render(
  send = vi.fn((input: FeedbackInput) =>
    of({
      queryId: input.queryId,
      rating: input.rating,
      categories: [...input.categories],
      remembered: input.rating === 1,
    }),
  ),
) {
  const fixture = TestBed.createComponent(FeedbackComponent);
  fixture.componentRef.setInput('queryId', QUERY_ID);
  fixture.componentRef.setInput('send', send);
  fixture.detectChanges();
  const element = fixture.nativeElement as HTMLElement;
  const click = (selector: string) => {
    element.querySelector<HTMLButtonElement>(selector)!.click();
    fixture.detectChanges();
  };
  const chip = (label: string) => {
    Array.from(element.querySelectorAll('button.chip'))
      .find((b) => b.textContent.trim() === label)!
      .dispatchEvent(new Event('click'));
    fixture.detectChanges();
  };
  return { element, click, chip, send, fixture };
}

describe('FeedbackComponent', () => {
  it('records a thumbs-up immediately and says it will be remembered', () => {
    const { element, click, send } = render();
    click('button[aria-label="Yes, this answer is right"]');
    expect(send).toHaveBeenCalledWith({
      queryId: QUERY_ID,
      rating: 1,
      categories: [],
      comment: null,
    });
    expect(element.textContent).toContain('help with similar questions');
    expect(
      element
        .querySelector('button[aria-label="Yes, this answer is right"]')
        ?.getAttribute('aria-pressed'),
    ).toBe('true');
  });

  it('collects reasons and a comment for a thumbs-down', () => {
    const { element, click, chip, send, fixture } = render();
    click('button[aria-label="No, this answer is wrong"]');
    expect(send).not.toHaveBeenCalled();
    chip('Wrong period');
    chip('Too slow');
    const textarea = element.querySelector('textarea')!;
    textarea.value = '  Should be last quarter  ';
    textarea.dispatchEvent(new Event('input'));
    element.querySelector('form')!.dispatchEvent(new Event('submit'));
    fixture.detectChanges();
    expect(send).toHaveBeenCalledWith({
      queryId: QUERY_ID,
      rating: -1,
      categories: ['wrong_period', 'slow'],
      comment: 'Should be last quarter',
    });
    expect(element.textContent).toContain('Thanks for the feedback.');
  });

  it('shows a failure', () => {
    const { element, click } = render(
      vi.fn(() => throwError(() => new BridgeError('NETWORK_ERROR', 'Cannot reach the server.'))),
    );
    click('button[aria-label="Yes, this answer is right"]');
    expect(element.querySelector('[role="alert"]')?.textContent).toContain(
      'Cannot reach the server.',
    );
  });
});
