import type { Routes } from '@angular/router';

import { authGuard, guestGuard } from './core/auth/auth.guards';

export const routes: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'chat' },
  {
    path: 'chat',
    title: 'Chat · ERP Insight AI',
    canActivate: [authGuard],
    loadComponent: () =>
      import('./chat/analytics-chat/analytics-chat.component').then(
        (m) => m.AnalyticsChatComponent,
      ),
  },
  {
    path: 'history',
    title: 'History · ERP Insight AI',
    canActivate: [authGuard],
    loadComponent: () =>
      import('./query-history/query-history.component').then((m) => m.QueryHistoryComponent),
  },
  {
    path: 'connections',
    title: 'Data sources · ERP Insight AI',
    canActivate: [authGuard],
    loadComponent: () =>
      import('./connections/connections-list.component').then((m) => m.ConnectionsListComponent),
  },
  {
    path: 'settings',
    title: 'Settings · ERP Insight AI',
    loadComponent: () =>
      import('./settings/connection-settings/connection-settings.component').then(
        (m) => m.ConnectionSettingsComponent,
      ),
  },
  {
    path: 'login',
    title: 'Sign in · ERP Insight AI',
    canActivate: [guestGuard],
    loadComponent: () => import('./auth/login/login.component').then((m) => m.LoginComponent),
  },
  { path: '**', redirectTo: 'chat' },
];
