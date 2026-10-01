import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';

import { App } from './app';
import { BackgroundBridgeService } from './core/messaging/background-bridge.service';
import type { ExtensionRequest, SessionInfo } from './core/messaging/protocol';

const SESSION: SessionInfo = {
  user: {
    id: 'u1',
    email: 'ana@acme.test',
    displayName: 'Ana Analyst',
    tenant: { id: 't1', slug: 'acme', name: 'Acme' },
    roles: ['analyst'],
    permissions: [],
  },
};

async function render(session: SessionInfo | null) {
  const request = vi.fn((message: ExtensionRequest) => {
    switch (message.type) {
      case 'auth.session':
        return of(session);
      case 'auth.logout':
        return of(null);
      default:
        return of({ status: 'ok', latencyMs: 1, checks: {}, checkedAt: '2026-01-01T00:00:00Z' });
    }
  });
  TestBed.configureTestingModule({
    imports: [App],
    providers: [
      provideRouter([
        { path: 'login', children: [] },
        { path: 'chat', children: [] },
      ]),
      { provide: BackgroundBridgeService, useValue: { request } },
    ],
  });
  const fixture = TestBed.createComponent(App);
  fixture.detectChanges();
  await fixture.whenStable();
  fixture.detectChanges();
  const element = fixture.nativeElement as HTMLElement;
  const navLinks = () =>
    Array.from(element.querySelectorAll('nav a')).map((a) => a.textContent.trim());
  return { fixture, element, navLinks, request };
}

describe('App shell', () => {
  it('renders the brand, skip link and signed-out navigation', async () => {
    const { element, navLinks } = await render(null);
    expect(element.querySelector('.brand__name')?.textContent).toContain('ERP Insight AI');
    expect(element.querySelector('.skip-link')?.getAttribute('href')).toBe('#main');
    expect(navLinks()).toEqual(['Sign in', 'Settings']);
    expect(element.querySelector('[data-testid="current-user"]')).toBeNull();
    expect(element.querySelector('main#main')).not.toBeNull();
  });

  it('shows the signed-in user, tenant and full navigation', async () => {
    const { element, navLinks } = await render(SESSION);
    expect(navLinks()).toEqual(['Chat', 'History', 'Data sources', 'Settings']);
    const user = element.querySelector('[data-testid="current-user"]');
    expect(user?.textContent).toContain('Ana Analyst');
    expect(user?.textContent).toContain('Acme');
  });

  it('signs out from the top bar', async () => {
    const { fixture, element, navLinks, request } = await render(SESSION);
    const button = Array.from(element.querySelectorAll('button')).find((b) =>
      b.textContent.includes('Sign out'),
    )!;
    button.click();
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(request).toHaveBeenCalledWith({ type: 'auth.logout' });
    expect(navLinks()).toEqual(['Sign in', 'Settings']);
  });
});
