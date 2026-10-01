// Generates the extension PNG icons (16/32/48/128) without external dependencies.
// Design: rounded azure square with three ascending white bars (analytics glyph).
// Usage: node scripts/generate-icons.mjs
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { deflateSync } from 'node:zlib';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const outDir = join(root, 'public', 'icons');

const CRC_TABLE = Array.from({ length: 256 }, (_, n) => {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});

function crc32(buf) {
  let c = 0xffffffff;
  for (const byte of buf) c = CRC_TABLE[(c ^ byte) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, 'ascii'), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body));
  return Buffer.concat([len, body, crc]);
}

function png(size, pixel) {
  const raw = Buffer.alloc(size * (size * 4 + 1));
  for (let y = 0; y < size; y++) {
    raw[y * (size * 4 + 1)] = 0; // filter: none
    for (let x = 0; x < size; x++) {
      const [r, g, b, a] = pixel(x + 0.5, y + 0.5, size);
      const o = y * (size * 4 + 1) + 1 + x * 4;
      raw[o] = r;
      raw[o + 1] = g;
      raw[o + 2] = b;
      raw[o + 3] = a;
    }
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // RGBA
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', deflateSync(raw)),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

function insideRoundedRect(x, y, s, radius) {
  const r = radius * s;
  const cx = Math.min(Math.max(x, r), s - r);
  const cy = Math.min(Math.max(y, r), s - r);
  return (x - cx) ** 2 + (y - cy) ** 2 <= r * r;
}

function pixel(x, y, s) {
  if (!insideRoundedRect(x, y, s, 0.22)) return [0, 0, 0, 0];
  const u = x / s;
  const v = y / s;
  const bars = [
    [0.22, 0.36, 0.58],
    [0.43, 0.57, 0.42],
    [0.64, 0.78, 0.26],
  ];
  for (const [x0, x1, top] of bars) {
    if (u >= x0 && u < x1 && v >= top && v < 0.78) return [255, 255, 255, 255];
  }
  // Diagonal azure -> teal gradient.
  const t = (u + v) / 2;
  return [Math.round(0 + 20 * t), Math.round(90 + 70 * t), Math.round(200 - 40 * t), 255];
}

mkdirSync(outDir, { recursive: true });
for (const size of [16, 32, 48, 128]) {
  writeFileSync(join(outDir, `icon-${size}.png`), png(size, pixel));
}
console.log(`Icons written to ${outDir}`);
