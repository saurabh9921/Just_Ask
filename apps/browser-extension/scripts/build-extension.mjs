// Assembles the unpacked extension in dist/ after `ng build`:
//  1. bundles the background service worker with esbuild (ESM, single file),
//  2. copies manifest.json,
//  3. verifies the side panel HTML contains no inline scripts or inline event
//     handlers (both are blocked by the MV3 extension CSP),
//  4. with --api-origin=<url> (or ERP_API_ORIGIN): points the extension at that API -
//     the only host it may call (manifest host_permissions) and the default API URL.
// Usage: node scripts/build-extension.mjs [--dev] [--api-origin=https://erp-insight.example.com]
import { build } from 'esbuild';
import { copyFileSync, existsSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const dist = join(root, 'dist');
const dev = process.argv.includes('--dev');

function fail(message) {
  console.error(`build-extension: ${message}`);
  process.exit(1);
}

const LOOPBACK = new Set(['localhost', '127.0.0.1', '[::1]']);

/** Same rules as src/app/core/settings/api-url.ts: HTTPS except for loopback hosts. */
function parseApiOrigin(raw) {
  let url;
  try {
    url = new URL(raw);
  } catch {
    fail(`--api-origin is not a valid URL: ${raw}`);
  }
  if (url.protocol !== 'https:' && !(url.protocol === 'http:' && LOOPBACK.has(url.hostname))) {
    fail('--api-origin must use https:// (http:// is allowed only for localhost)');
  }
  if (url.username || url.password || url.search || url.hash) {
    fail('--api-origin must not contain credentials, a query or a fragment');
  }
  return {
    baseUrl: `${url.origin}${url.pathname}`.replace(/\/+$/, ''),
    hostPermission: `${url.origin}/*`,
  };
}

const originArg = process.argv.find((a) => a.startsWith('--api-origin='));
const originRaw =
  (originArg ? originArg.slice('--api-origin='.length) : process.env.ERP_API_ORIGIN) || '';
const api = originRaw.trim() ? parseApiOrigin(originRaw.trim()) : null;

if (!existsSync(join(dist, 'index.html'))) {
  fail('dist/index.html not found - run `ng build` first.');
}

// Mirrors Angular's fileReplacements for the development configuration.
const devEnvironmentPlugin = {
  name: 'dev-environment',
  setup(pluginBuild) {
    pluginBuild.onResolve({ filter: /\/environments\/environment$/ }, () => ({
      path: join(root, 'src', 'environments', 'environment.development.ts'),
    }));
  },
};

// Bakes the API origin into the background worker's default settings.
const apiOriginPlugin = {
  name: 'api-origin',
  setup(pluginBuild) {
    pluginBuild.onLoad(
      { filter: /[\\/]environments[\\/]environment(\.development)?\.ts$/ },
      (args) => {
        const source = readFileSync(args.path, 'utf8');
        const replaced = source.replace(
          /defaultApiBaseUrl:\s*'[^']*'/,
          `defaultApiBaseUrl: ${JSON.stringify(api.baseUrl)}`,
        );
        if (replaced === source) fail(`defaultApiBaseUrl not found in ${args.path}`);
        return { contents: replaced, loader: 'ts' };
      },
    );
  },
};

await build({
  entryPoints: [join(root, 'src', 'background', 'main.ts')],
  outfile: join(dist, 'background.js'),
  bundle: true,
  format: 'esm',
  platform: 'browser',
  target: 'chrome116',
  minify: !dev,
  sourcemap: dev ? 'inline' : false,
  tsconfig: join(root, 'tsconfig.background.json'),
  plugins: [...(dev ? [devEnvironmentPlugin] : []), ...(api ? [apiOriginPlugin] : [])],
  logLevel: 'warning',
  legalComments: 'none',
});

copyFileSync(join(root, 'manifest.json'), join(dist, 'manifest.json'));
if (api) {
  const built = JSON.parse(readFileSync(join(dist, 'manifest.json'), 'utf8'));
  // The extension may talk to the configured API only.
  built.host_permissions = [api.hostPermission];
  writeFileSync(join(dist, 'manifest.json'), `${JSON.stringify(built, null, 2)}\n`);
}

const html = readFileSync(join(dist, 'index.html'), 'utf8');
const inlineScript = /<script(?![^>]*\bsrc=)[^>]*>/i;
const inlineHandler = /\son[a-z]+\s*=/i;
if (inlineScript.test(html)) fail('dist/index.html contains an inline <script>.');
if (inlineHandler.test(html)) fail('dist/index.html contains an inline event handler.');

const manifest = JSON.parse(readFileSync(join(dist, 'manifest.json'), 'utf8'));
for (const file of [
  manifest.background.service_worker,
  manifest.side_panel.default_path,
  ...Object.values(manifest.icons),
]) {
  if (!existsSync(join(dist, file))) fail(`manifest references missing file: ${file}`);
}

console.log(
  `Extension assembled in ${dist}${dev ? ' (development)' : ''}. Load it via chrome://extensions > Load unpacked.`,
);
if (api) {
  console.log(`API: ${api.baseUrl} (host permission ${api.hostPermission})`);
} else if (!dev) {
  console.warn(
    'build-extension: no --api-origin given - this build can only reach http://localhost:8080. ' +
      'For production run: npm run build -- --api-origin=https://your-api-host',
  );
}
