import type { Environment } from './environment.model';

export const environment: Environment = {
  production: false,
  defaultApiBaseUrl: 'http://localhost:8080',
  healthPollIntervalMs: 30_000,
  healthCheckTimeoutMs: 5_000,
  bridgeTimeoutMs: 20_000,
  apiTimeoutMs: 15_000,
};
