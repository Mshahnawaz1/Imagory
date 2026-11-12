# Imagory website: design and plan

The plan for the Imagory website, a small static site that shows what the app does, hosts the
user docs and links to the downloads. Status: **phase 2 (build)**, tracked in `todo.md`.

- **Domain:** `https://imagory.mshahnawaz.me` (a Vercel subdomain)
- **Repo:** this repo, in `frontend/` (see https://github.com/Mshahnawaz1/imagory)
- **Reference:** https://themia.app. We follow its structure and feel, but use Imagory's own
  content, colours and assets. We copy none of its text, images or code.

## 1. Goals

1. Explain what Imagory is in one screen: an offline photo browser for local folders and
   Google Takeout exports.
2. Get people to the right download: Windows `.exe` or Linux `.tar.gz`, both on GitHub
   Releases.
3. Host short, user-facing docs: getting started, Takeout, cleanup, troubleshooting.
4. Measure page visits and downloads.
5. Stay simple: static pages, no backend, almost no JavaScript.

Out of scope: pricing, accounts, a newsletter, a blog, translations, search, and a docs
framework (no Starlight).

## 2. Tech stack

| Choice | Decision |
| --- | --- |
| Framework | **Astro 7**, static output (`output: "static"`). Vercel needs no adapter for static sites |
| Styling | Plain CSS with custom properties (the tokens in section 4) and Astro scoped styles. No Tailwind |
| Interactivity | Small vanilla scripts only: theme toggle, mobile nav, screenshot carousel. The FAQ uses `<details>`. No React or other framework islands |
| Content | Astro content collections (`glob` loader) for `docs` and `changelog`, written in Markdown |
| Images | `astro:assets` (`<Image>`/`<Picture>`) for screenshots, served as AVIF/WebP with set sizes |
| Fonts | Self-hosted through `@fontsource`, so the site makes no Google Fonts request |
| SEO | `@astrojs/sitemap`, per-page `<title>` and description, Open Graph image, `robots.txt` |
| Analytics | **Vercel Web Analytics** (`@vercel/analytics`), see section 7 |
| Package manager | npm, with a committed `package-lock.json` |
| Node | 22 LTS (`"engines"` in `package.json`, and the matching setting on Vercel) |

## 3. Site map

| Route | Page |
| --- | --- |
| `/` | Home (landing page, section 5) |
| `/docs` | Docs index: grouped list of links, like Themia's |
| `/docs/getting-started` | Download, first run, add your first folder |
| `/docs/sources` | Adding, renaming, relocating, rescanning and removing sources; missing files |
| `/docs/takeout-import` | Using an extracted Takeout export: dates, places, people, Trash skipped |
| `/docs/takeout-cleanup` | Clean up Takeout: what it makes, what it never touches, the report |
| `/docs/windows-warnings` | SmartScreen and antivirus warnings on the unsigned `.exe`, and how to proceed |
| `/docs/troubleshooting` | Common problems, including Linux `libxcb-cursor0`, where logs and the library live |
| `/docs/faq` | Full FAQ (the home page shows a shorter version) |
| `/changelog` | Releases, newest first, from the `changelog` collection |
| `/privacy` | Two parts: the **app** is fully offline; the **website** uses cookieless analytics |
| `/download/windows`, `/download/linux` | "Thanks for downloading" page that starts the download (section 6) |
| `/404` | Not found |

Docs layout: a left sidebar with the groups below, the content in the middle, and an "On this
page" list on wide screens. Each page has previous/next links and an "Edit on GitHub" link.

- **Getting started:** Getting started, Windows warnings
- **Using Imagory:** Sources, Takeout import, Takeout cleanup
- **Help:** Troubleshooting, FAQ, Privacy

Docs are written for users and taken from `README.md` and `docs/takeout-cleanup.md`. They are
not copies of the developer docs. When user-facing behaviour changes, update the site docs
as well as the README (see `CLAUDE.md`).

## 4. Visual design

### 4.1 Colour tokens (from `imagory/ui/theme.py`)

The site follows the visitor's system theme and has a toggle in the header (remembered in
`localStorage`, wrapped in try/catch).

| Token | Light | Dark | Use |
| --- | --- | --- | --- |
| `--forest` | `#10291F` | `#BFE24B` | Primary buttons, strong text (it turns lime in dark mode, as in the app) |
| `--on-forest` | `#F7F5EF` | `#10291F` | Text on primary buttons |
| `--bg` | `#F7F5EF` (cream) | `#0B1A15` (paper) | Page background |
| `--paper` | `#EFEDE6` | `#0B1A15` | Alternate section bands |
| `--surface` | `#FBFAF6` | `#10241D` | Cards, header |
| `--surface-alt` | `#F3F1EA` | `#163028` | Hover, code blocks, sidebar |
| `--line` | `#DCD9CE` | `#24413A` | Borders, dividers |
| `--text` | `#10291F` | `#F1EFE7` | Body text |
| `--muted` | `#6B7568` | `#8FA397` | Secondary text, captions |
| `--selection` | `#2F6B3F` | `#BFE24B` | Links, focus rings |
| `--accent` | `#BFE24B` | `#BFE24B` | Highlights, chips, the "new" badge |
| `--viewer` | `#0B1A15` | `#0B1A15` | Screenshot frames (the viewer background) |

Lime (`#BFE24B`) on cream fails contrast for text. Use it only for fills behind forest text,
never as a text colour in light mode.

### 4.2 Type and shape

- **Font:** *Geist* (variable, self-hosted, OFL), the typeface Vercel uses on its own site.
  One family for everything. *Geist Mono* is used only for real file names, paths and code,
  never for labels or numbers.
- **Scale:** 15 px body, line height 1.6. h1 `clamp(2.1rem, 4.8vw, 3.4rem)`, h2
  `clamp(1.55rem, 3vw, 2.2rem)`, h3 1rem. Headings are weight 600 with -0.02 to -0.03em
  tracking and `text-wrap: balance`.
- **Corners are square** on buttons, cards and frames, matching the app's square logo mark
  and the reference.
- **Buttons:** solid forest (lime in dark mode), OS icon, the version in muted tabular
  figures on the right. The hero download buttons are compact (248 × 34 px). Secondary buttons are outlined.
- **Hero:** split layout, text left on a faint dot grid, the app screenshot bleeding off
  the right edge, and one grey line of platform facts under the buttons.
- **Feature tabs:** four compact bordered cards (max 256 px wide, centred) that work as
  tabs. Each selects a different app screenshot in the stage below: welcome screen,
  cleanup preview, grid, viewer with Takeout info. Arrow keys move between them.
- **Showcase:** slanted panels of real app screenshots, each with a plain caption.
- **Performance:** a plain table of target vs measured figures, not big-number stat cards.
- **Bands** alternate `--bg` and `--paper`.

Avoid, because they make the page read as generated: uppercase labels above headings, a
single highlighted word in headings, `01/02` numbering on content that is not a sequence,
monospace for small labels, scroll fade-ins on every section, arrows appended to links,
rows of decorative chips or dot-bullet trust rows.

App screenshots: the welcome screen (`references/image-1.png`) and the Sources dialog
(`image-3.png`) are fine to show. `image-4.png` and `image-5.png` show **real people and
their names**. Never publish them; the main-window screenshots are taken from a synthetic
library instead (see `todo.md`).

### 4.3 Assets

- Logo: `assets/imagory-logo.svg` (light) and `assets/imagory-logo-dark.svg` (dark).
  Favicon: `assets/imagory-mark.svg`. Copied into `frontend/public/` or `src/assets/`
  when the project is set up.
- Screenshots: taken from the real app showing a **generated fake library**
  (`scripts/make_fake_library.py`), never real personal photos. Until then the layout uses
  placeholders that are the same size.
- Open Graph image: 1200×630, logo and tagline on forest.

## 5. Home page, top to bottom

Follows Themia's order. Pricing becomes a "Free and open source" download band, and there is
no newsletter.

1. **Announcement bar** (optional, can be turned off): "Imagory 0.2 is out, now with a
   Linux build → Changelog".
2. **Sticky header:** logo, nav (Features, Docs, Changelog, GitHub), theme toggle, and a
   **Download** button that jumps to the download band. A menu button on phones.
3. **Hero**
   - H1, for example: "Your photo library, offline and organised."
   - Subhead: browse local folders and Google Takeout exports, fast, without uploading
     anything.
   - CTAs: **Download for Windows** with a `v0.2.0` chip (primary) and **Download for
     Linux** (secondary). A small "Other downloads and checksums → GitHub Releases" link.
   - Trust line: "Windows 10 & 11 · Linux x86_64 · Free and open source · No account".
   - Large hero screenshot: the grid with month headers.
4. **Feature strip** (4 items, each an icon and a short line):
   - **Fully offline**: no uploads, no account, no telemetry in the app.
   - **Never touches your photos**: it only reads them.
   - **Fast**: smooth scrolling through 100,000 photos, and rescans that only look at what
     changed.
   - **Made for Google Takeout**: dates, places and people from the JSON files.
5. **Showcase:** a full-width screenshot of the main window with numbered callouts
   (sources sidebar, filters, month headers, info panel). A chip row below: "Search ·
   Filters · Albums · People · Duplicates · Light & dark".
6. **"Fast with big libraries"** section: a short claim with 2 or 3 numbers, using only
   figures measured against PRD §10, such as the scan rate on the 100k fake library and how
   long the grid takes to open. A screenshot or short loop of the grid scrolling.
7. **"Turn a messy Takeout into a tidy folder"** (Clean up Takeout): before/after carousel
   (Takeout folder full of JSON → `YYYY/MM` folders), bullets: copies only, every copy
   verified, metadata kept in `metadata.json`.
8. **Feature grid** "Everything in one place" (2×3 cards): exact duplicates shown once,
   dates from Takeout/EXIF/filename, search and filters, albums, people from Takeout,
   viewer with zoom and info panel.
9. **Download band** "Free and open source": two cards side by side.
   - **Windows:** `Imagory.exe`, Windows 10 and 11, 64-bit, about 50 MB, no install.
   - **Linux:** `Imagory-<version>-linux-x86_64.tar.gz`, Ubuntu 22.04+ or similar, X11 or
     XWayland.
   - Trust row: "MIT licence · No account · Works offline · Source on GitHub".
   - A note: "Windows may warn about an unsigned app → How to run it" (links to
     `/docs/windows-warnings`).
10. **FAQ** (6 to 8 `<details>` items): Is it really offline? Does it change my photos?
    Does it work with Takeout zips? (Not yet: extract them first.) Why does Windows warn me?
    Where is my library stored? Does it find faces? (No.) Is it free? Then "More in the
    FAQ".
11. **Footer:** logo and tagline, columns **Product** (Download, Features, Changelog),
    **Docs** (Getting started, Takeout cleanup, Troubleshooting), **Project** (GitHub,
    Report a bug, Licence, Privacy). "© 2026 Imagory · MIT licence".
12. **Sticky bottom bar** (desktop only, can be closed): "v0.2.0 → Changelog · Windows 10/11 ·
    Linux".

## 6. Downloads and version

All version data lives in one typed file, `frontend/src/site.ts`:

```ts
export const release = {
  version: "0.2.0",
  tag: "v0.2.0",            // the GitHub tag; check it matches when publishing
  date: "2026-11-xx",
  windows: { file: "Imagory.exe", sizeMb: 49 },
  linux: { file: "Imagory-0.2.0-linux-x86_64.tar.gz" },
};
export const repo = "https://github.com/Mshahnawaz1/imagory";
```

- Download URL: `${repo}/releases/download/${tag}/${file}`. This is a direct link to the
  file, so it always matches the version shown on the button.
- "All releases" link: `${repo}/releases`.
- Each download button goes to `/download/windows` or `/download/linux`. That page says
  "Thanks for downloading Imagory", starts the download after about 1 second (a meta refresh
  plus a script, and a "Click here if it didn't start" link), and shows the next steps (the
  SmartScreen note on Windows; unpack and `./Imagory` on Linux). This copies Themia's
  thank-you panel, and it lets the free analytics tier count downloads as page views.
- To release: edit `site.ts`, add a changelog entry, and push. Vercel rebuilds the site.
- **Before launch:** no GitHub release is published yet (the API returns "Not Found" for
  `releases/latest`), so the download links will 404 until 0.2.0 is published with these
  exact asset names and tag.

## 7. Analytics

- **Vercel Web Analytics** with the `<Analytics />` component in the base layout. It's
  cookieless and stores no personal data, so the site needs no cookie banner. It counts page
  views, referrers, countries and devices.
- **Downloads** are counted two ways:
  1. page views of `/download/windows` and `/download/linux` (works on the free Hobby plan);
  2. GitHub's own `download_count` for each release asset (the exact number of files
     downloaded, visible through the GitHub API).
- Custom click events (`track("download", …)`) need Vercel Pro. Not used for now.
- Optional: Vercel Speed Insights for Core Web Vitals.
- **The privacy promise stays accurate.** The *app* is fully offline (PRD §11). The
  *website* uses analytics, so the site must never say "no tracking". It says "the app
  makes no network connections", and `/privacy` lists what the website measures.

## 8. Project layout

```
frontend/
  design.md                 this file
  README.md                 dev commands, deploy notes
  package.json, package-lock.json, astro.config.mjs, tsconfig.json
  public/                   favicon.svg, og.png, robots.txt
  src/
    site.ts                 version, download URLs, nav and footer links
    styles/tokens.css       colour, type and spacing tokens (section 4)
    styles/global.css
    layouts/Base.astro      head, header, footer, analytics, theme script
    layouts/Docs.astro      sidebar, table of contents, prev/next
    components/             Header, Footer, Hero, FeatureStrip, Showcase, Carousel,
                            FeatureGrid, DownloadBand, DownloadButton, Faq, VersionBar,
                            ThemeToggle, Screenshot
    content.config.ts       docs and changelog collections
    content/docs/*.md
    content/changelog/*.md  one file per release (0.2.0 from docs/release-notes-0.2.0.md)
    assets/screenshots/
    pages/index.astro, docs/index.astro, docs/[...slug].astro, changelog.astro,
          privacy.astro, download/[os].astro, 404.astro
```

## 9. Deployment (Vercel)

1. Import the GitHub repo in Vercel and set **Root Directory** to `frontend`. Framework
   preset: Astro, build `npm run build`, output `dist`.
2. **Ignored Build Step:** `git diff HEAD^ HEAD --quiet -- .`, so commits that only touch
   the Python app don't rebuild the site.
3. Turn on Web Analytics in the project settings.
4. Domain: add `imagory.mshahnawaz.me` in Vercel, then add a DNS record at the provider for
   `mshahnawaz.me`: `CNAME imagory → cname.vercel-dns.com`. Vercel issues the HTTPS
   certificate.
5. Set `site: "https://imagory.mshahnawaz.me"` in `astro.config.mjs` (for the sitemap and
   canonical URLs).
6. Pull requests get Vercel preview URLs automatically.

## 10. Phases

1. **Plan** (this file). Then update it with the screenshots: fonts, spacing, hero layout,
   card style.
2. **Scaffold:** Astro project, tokens, base layout, header and footer, theme toggle,
   `site.ts`.
3. **Home page** with placeholder screenshots, every section in section 5, and a phone
   layout at 360 px.
4. **Docs, changelog, privacy, download pages, 404.**
5. **Real screenshots** from the fake library, the OG image, measured performance numbers.
6. **Launch:** analytics, Vercel project, domain, a Lighthouse pass (target 95+ in every
   category), a link check, and a link to the site in `README.md`.

Checks for every phase: `npm run build` and `npx astro check` pass with no errors, and the
pages work in both themes and at 360 px wide.

## 11. Open points

- Final fonts and hero layout: decided after the reference screenshots.
- Tag naming (`v0.2.0` or `0.2.0`) to confirm when the first release is published.
- Whether to keep the announcement bar and the bottom version bar, or only one of them.
