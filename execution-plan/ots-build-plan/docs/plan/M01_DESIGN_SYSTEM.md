# M01 — Design System (white canvas, rounded, Fibonacci)

**Depends on:** M00.2 (assets build stage).
**Produces:** Tailwind configuration with tokens, a base layout, and a server-rendered component library (Django template partials), used by every screen.

## Tokens (`static_src/tailwind.config.js`)

| Token | Values |
| --- | --- |
| Colours | `canvas #FFFFFF`; `ink-900 #111111`; `ink-600 #555555`; `ink-400 #767676` (was #8A8A8A, 3.45:1: below AA); `line #E8E8E8`; `accent #2F3E56` (deep slate blue, primary actions only); `ok #2E7D4F`; `bad #B23A3A`; `warn #A16A1B` (was #B7791F, 3.64:1: below AA). **No other colours.** |
| Spacing (px) | `0.5:3, 1:5, 2:8, 3:13, 4:21, 5:34, 6:55, 7:89, 8:144` (replaces the Tailwind default scale) |
| Radius (px) | `sm:8` (inputs, chips), `md:13` (cards, table containers), `lg:21` (panels, modals), `full` (buttons, badges, avatars). `rounded-none` is **disabled** by a lint rule that fails the build if it appears in templates |
| Type (px / line height) | `cap 13/18`, `body 16/24`, `sec 21/29`, `title 26/34`, `fig 34/40`. Font Inter (self-hosted woff2, weights 400, 500 and 600) |
| Shadow | `card: 0 1px 3px rgba(0,0,0,0.06)` only |
| Motion | `fast 144ms`, `panel 233ms`, ease-out. `prefers-reduced-motion` turns motion off |
| Layout | sidebar `233px` (collapsed `55px`); page margin `34px`; card gap `21px`; card padding `21px`; list/detail split `grid-cols-[38.2fr_61.8fr]`; max width 1440px |
| Table | rounded `md` container, row height 55px, text left-aligned, numbers right-aligned (`tabular-nums`) |

## Components (`templates/components/*.html`, included with `{% include %}` + `with`)

| Component | Notes |
| --- | --- |
| `button.html` | variants: primary (accent), secondary (outline), danger (bad), ghost; `rounded-full`; loading state disables the button and adds an `Idempotency-Key` (see M09) |
| `card.html` | title slot, body, footer |
| `badge.html` | status chips with a fixed mapping enum → colour (`ok` / `warn` / `bad` / neutral) |
| `table.html` | header row, sortable columns (HTMX), empty state, pagination (cursor-based) |
| `form_field.html` | label above, help text, error text in `bad`; inputs `rounded-sm`, 1px `line` border, focus ring `accent` |
| `modal.html` | `rounded-lg`, focus trap with Alpine, Esc closes |
| `drawer.html` | right-side panel, 38.2% wide, used for attendance day detail and file trail |
| `toast.html` | bottom-right; success / error / warning; auto-dismiss after 5 s except errors |
| `avatar.html` | circle; initials fallback |
| `empty_state.html` | icon (Lucide) + one line + optional action |
| `pending_chip.html` | "N changes waiting" (M09) |
| `offsite_badge.html` | "Not yet off-site" for files not in ARCHIVED |

**Base layout (`templates/base.html`):**

- sidebar: role-specific navigation;
- top bar: page title, notification bell, user menu;
- content area;
- toast region;
- HTMX config: `hx-headers` carries the CSRF token; `htmx.config.defaultSwapStyle='outerHTML'`;
- Alpine;
- service worker registration (M09).

**Accessibility:** WCAG AA contrast for every text/background pair (test with a script), focus-visible rings, labelled form fields, and keyboard-reachable menus.

## Steps

### M01.1 — Tokens and build

**Prompt**
```
Read CLAUDE.md and docs/plan/M01_DESIGN_SYSTEM.md. Implement Step M01.1.
Write static_src/tailwind.config.js with exactly these tokens, replacing the default spacing, radius, colour and font-size scales (no extend for these). Self-host Inter woff2 in static/fonts with an @font-face in static_src/app.css.
Add scripts/check_templates.py, run by `make lint`. It fails if any template contains rounded-none, a hex colour outside the token list, inline style colour, or an http(s):// asset URL.
Add a contrast test (tests/design/test_contrast.py) computing WCAG ratios for ink-* on canvas and white on accent/ok/bad/warn. All must be ≥ 4.5.
```

### M01.2 — Base layout and components

**Prompt**
```
Implement Step M01.2: templates/base.html and every component in the component table, as Django template partials with documented parameters at the top of each file.
Build a /styleguide page, visible to SUPER_ADMIN only once M02 exists (until then, DEBUG only), that renders every component in every state.
Vendor htmx 2, alpine 3 and lucide icons (svg sprite) under static/vendor. No CDN.
Test: render /styleguide and assert no external URLs appear in the HTML.
```

### M01.3 — Screen shells

The following page shells use placeholder content. Modules fill them later.

- Login (centred 377px card, circular camera preview area).
- Dashboard grid (stat cards in rows of 3 or 5).
- List/detail split page.
- Profile page with tabs.
- Settings page.
- 403, 404 and 500 pages.

**Prompt**
```
Implement Step M01.3: page shells for login, dashboard, list/detail split (38.2/61.8), profile with tabs, settings, and error pages 403/404/500, all using M01 components only. Each shell has a snapshot test asserting the key structural classes.
```

## M01 done-gate

- [ ] `make lint` enforces the template rules.
- [ ] The style guide renders.
- [ ] No external assets.
- [ ] Contrast test passes.
