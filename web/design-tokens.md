# MathTranslate Local Web UI — Design Tokens

## 1. Aesthetic Direction: Academic Editorial (Warm Paper & Ink)

This interface is modeled after a mathematical journal desk and archival typesetting workbench: warm paper surfaces, crisp hairlines, deep ink typography, and calm, high-contrast academic accents.

### Explicit Non-Goals (Anti-Patterns Avoided)
- **No purple-blue AI gradients** (replaces `reference/web`'s `.bg-gradient` linear-gradient).
- **No decorative emojis** in headings, buttons, tabs, or status messages.
- **No generic 3-equal-column feature cards**.
- **No default unstyled Bootstrap look** and **no external CDN dependencies** (works 100% offline/locally).
- **No nested `<form>` tags** or bouncy hover animations.

---

## 2. Color Palette

| Token | Hex | Role |
|---|---|---|
| `--bg-paper` | `#FAF9F5` | Primary page canvas (warm paper) |
| `--bg-surface` | `#FFFFFF` | Elevated cards and form controls |
| `--bg-recessed` | `#F2F0E8` | Recessed code blocks, job log console, inactive tab well |
| `--bg-hover` | `#F5F3EC` | Subtle interactive hover surface |
| `--text-ink` | `#18181B` | Primary headings, labels, body copy (deep ink) |
| `--text-secondary` | `#52525B` | Secondary descriptions, helper text, metadata |
| `--text-muted` | `#71717A` | Placeholders, timestamps, subtle captions |
| `--accent-primary` | `#1B3A5C` | Primary CTA, active tab underline/indicator, focus accents (academic slate-navy) |
| `--accent-primary-hover` | `#142C47` | Primary button hover state |
| `--accent-primary-soft` | `#EDF2F7` | Soft tint for selected dropzones and subtle badges |
| `--status-success` | `#1F6E43` | Success text, icon stroke, border accent (forest) |
| `--status-success-bg` | `#EEF7F2` | Success banner/badge background |
| `--status-error` | `#9B2C2C` | Error text, validation alert, failure badge (terracotta/crimson) |
| `--status-error-bg` | `#FDF2F2` | Error banner/badge background |
| `--status-running` | `#8A5314` | Running/queued progress text and indicator |
| `--status-running-bg` | `#FBF5EB` | Running/queued badge background |
| `--border-hairline` | `#E2DFD5` | Default 1px structural border |
| `--border-strong` | `#C8C3B4` | Input borders on hover and prominent dividers |

---

## 3. Typography

| Token | Value | Usage |
|---|---|---|
| `--font-serif` | `Georgia, "Times New Roman", serif` | Brand masthead, page title, panel headings |
| `--font-sans` | `"Segoe UI", system-ui, -apple-system, sans-serif` | Form labels, controls, tabs, buttons, status copy |
| `--font-mono` | `"Consolas", "Cascadia Mono", ui-monospace, monospace` | arXiv IDs, filenames, language codes, live terminal log |

### Type Scale
- Masthead title: `1.625rem` (26px), `font-weight: 700`, `line-height: 1.2`, `--font-serif`, letter-spacing `-0.01em`
- Section heading: `1.125rem` (18px), `font-weight: 600`, `line-height: 1.3`, `--font-serif`
- Body & inputs: `0.9375rem` (15px), `line-height: 1.5`, `--font-sans`
- Labels & controls: `0.8125rem` (13px), `font-weight: 600`, `line-height: 1.4`, `--font-sans`
- Captions & metadata: `0.75rem` (12px), `line-height: 1.4`, `--font-sans`
- Monospace log & IDs: `0.8125rem` (13px), `line-height: 1.55`, `--font-mono`

---

## 4. Spacing Rhythm (4px Base Grid)

| Token | Value | Usage |
|---|---|---|
| `--space-1` | `4px` | Micro gaps (label to helper hint) |
| `--space-2` | `8px` | Compact spacing (badge padding, icon-to-label gap) |
| `--space-3` | `12px` | Input padding, stacked list gaps |
| `--space-4` | `16px` | Field vertical rhythm, card internal gap |
| `--space-6` | `24px` | Card padding, column gap |
| `--space-8` | `32px` | Page vertical breathing room, header separation |

---

## 5. Borders, Radii & Elevation

| Token | Value | Usage |
|---|---|---|
| `--radius-sm` | `4px` | Inputs, buttons, badges, tab items, code blocks |
| `--radius-md` | `6px` | Primary workspace cards, dropzones, status banners |
| `--shadow-flat` | `0 1px 2px rgba(24, 24, 27, 0.04)` | Minimal paper-on-desk elevation |
| `--focus-ring` | `0 0 0 2px #FAF9F5, 0 0 0 4px #1B3A5C` | Accessible keyboard focus indicator |

---

## 6. Layout Architecture & Breakpoints

- **Container max-width:** `1160px`, centered with `24px` horizontal padding (`16px` on mobile).
- **Desktop (`> 768px`):** Asymmetric 2-column CSS Grid:
  - Left column (`minmax(360px, 440px)`): Focused translation workbench (mode tabs, input field/dropzone, engine selector, source/target language selectors, Auto PDF toggle, single primary submit action).
  - Right column (`1fr`): Live job status panel (4-state UI), downloadable artifacts shelf, and recessed monospace execution log.
- **Mobile (`<= 768px`):** Collapses cleanly to a single stacked column (`1fr`), full-width controls, comfortable touch targets (`>= 40px` height).

---

## 7. Motion & Interaction Style

- **Duration:** `--transition-fast: 150ms ease` for borders, backgrounds, and text color changes.
- **No layout-shifting transforms** (no `translateY(-3px)` button bounce from `reference/web`).
- **Running indicator:** Subtle opacity pulse (`1.4s ease-in-out infinite`) on a 8px solid dot; respects `prefers-reduced-motion: reduce`.

---

## 8. 4-State UI Contract (Right Workspace Panel)

1. **Idle / Empty (`data-state="idle"`):** Crisp editorial empty state explaining the three supported modes (arXiv ID, single `.tex`, multi-file `.zip`) and XeLaTeX PDF behavior.
2. **Running / Loading (`data-state="running"`):** Active status pill (`Queued` / `Running`), job ID in monospace, live-updating recessed terminal log polling `GET /api/jobs/<job_id>`.
3. **Success (`data-state="done"`):** Forest-green completion banner, PDF compilation status note (compiled / skipped / missing XeLaTeX hint for Overleaf), direct download links for every generated artifact, plus full log.
4. **Error (`data-state="error"`):** Terracotta-crimson alert banner displaying the concrete error summary alongside the full captured log for diagnosis.
