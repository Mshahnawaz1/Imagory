// Renders public/og.png (1200x630) from an inline SVG using sharp.
// Usage: node scripts/make-og.mjs   (from the frontend/ folder)
import { writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import sharp from "sharp";

const out = fileURLToPath(new URL("../public/og.png", import.meta.url));

// The three shapes of assets/imagory-mark.svg (32-unit grid), scaled to 300 px and
// placed on the left, vertically centred. Its forest square matches the background,
// so only the lime block and cream "I" show.
const MARK = 300; // px, the mark's size on the card
const S = MARK / 32;
const mx = 40; // the visible lime block and "I" start at x ≈ 143
const my = 165; // (630 - 300) / 2 would centre it; the footer line sits below it
const at = (x, y) => `${(mx + x * S).toFixed(2)} ${(my + y * S).toFixed(2)}`;
const rect = (x, y, w, h, fill) =>
  `<rect x="${(mx + x * S).toFixed(2)}" y="${(my + y * S).toFixed(2)}" width="${(w * S).toFixed(2)}" height="${(h * S).toFixed(2)}" fill="${fill}"/>`;

const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">
  <rect width="1200" height="630" fill="#10291F"/>
  ${rect(3, 3, 26, 26, "#10291F")}
  ${rect(11, 9.5, 6, 5, "#BFE24B")}
  <path d="M${at(11, 17)} h${(6 * S).toFixed(2)} v${(8 * S).toFixed(2)} h${(-6 * S).toFixed(2)} z" fill="#F7F5EF"/>
  <text x="270" y="330" font-family="Inter, Helvetica, Arial, sans-serif" font-size="128" font-weight="700" letter-spacing="-4" fill="#F7F5EF">Imagory</text>
  <rect x="270" y="368" width="120" height="8" fill="#BFE24B"/>
  <text x="270" y="430" font-family="Inter, Helvetica, Arial, sans-serif" font-size="34" font-weight="400" fill="#F7F5EF">Your photo library, offline and organised.</text>
  <text x="143" y="560" font-family="Inter, Helvetica, Arial, sans-serif" font-size="26" fill="#BFE24B">Windows · Linux · Free and open source</text>
</svg>`;

await writeFile(out, await sharp(Buffer.from(svg)).png().toBuffer());
console.log(`Wrote ${out}`);
