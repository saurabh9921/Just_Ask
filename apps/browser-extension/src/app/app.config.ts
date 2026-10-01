import {
  provideBrowserGlobalErrorListeners,
  provideZonelessChangeDetection,
  type ApplicationConfig,
} from '@angular/core';
import { provideRouter, withHashLocation } from '@angular/router';

import { routes } from './app.routes';

export const appConfig: ApplicationConfig = {
  providers: [
    provideBrowserGlobalErrorListeners(),
    provideZonelessChangeDetection(),
    // Extension pages are served from a static file, so hash routing avoids
    // 404s on reload and needs no server-side rewrite rules.
    provideRouter(routes, withHashLocation()),
  ],
};
