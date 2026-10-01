import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatTabsModule } from '@angular/material/tabs';

import { WorkspaceService } from './connections/workspace.service';
import { AuthService } from './core/auth/auth.service';
import { ConnectionStatusIndicatorComponent } from './core/status/connection-status-indicator.component';

interface NavItem {
  readonly path: string;
  readonly label: string;
}

const SIGNED_IN_NAV: readonly NavItem[] = [
  { path: '/chat', label: 'Chat' },
  { path: '/history', label: 'History' },
  { path: '/connections', label: 'Data sources' },
  { path: '/settings', label: 'Settings' },
];
const SIGNED_OUT_NAV: readonly NavItem[] = [
  { path: '/login', label: 'Sign in' },
  { path: '/settings', label: 'Settings' },
];

@Component({
  selector: 'eia-root',
  imports: [
    RouterOutlet,
    RouterLink,
    RouterLinkActive,
    MatButtonModule,
    MatTabsModule,
    ConnectionStatusIndicatorComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './app.html',
  styleUrl: './app.scss',
})
export class App {
  protected readonly auth = inject(AuthService);
  private readonly workspace = inject(WorkspaceService);
  private readonly router = inject(Router);

  protected readonly nav = computed(() => (this.auth.signedIn() ? SIGNED_IN_NAV : SIGNED_OUT_NAV));

  constructor() {
    void this.auth.ensureLoaded();
  }

  protected focusMain(event: Event): void {
    event.preventDefault();
    document.getElementById('main')?.focus();
  }

  protected logout(): void {
    this.auth.logout().subscribe(() => {
      this.workspace.reset();
      void this.router.navigate(['/login']);
    });
  }
}
