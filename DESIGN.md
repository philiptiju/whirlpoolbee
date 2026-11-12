# WhirlpoolBee — Design System

<!-- impeccable:design-schema 1 · seed d4153a89 · recorded 2026-10-07 -->
<!-- Ground truth: src/web/static/css/style.css + base.html + overview.html -->

## World

**SOLARI vernacular ephemera.** WhirlpoolBee is a departures board for AI development work. Sessions track like flights — departure time, destination (project), duration, cost, status. The dominant tone is warm off-white shell against a near-black gate board. Navy is reserved for the shell crown and live-state accents. Amber signals only anomaly and live status. The world refuses the glowing-accent dark-card template at every surface; depth comes from tonal contrast between the warm shell ground and the near-black gate board, not from shadows or glows.

---

## Color Tokens

All values are as defined in `:root` in `style.css`. The direction contract specified `--ink: #1A1814`; the build ships `#0D0F14` — the cooler near-black is canonical.

| Token | Value | Role |
|---|---|---|
| `--ink` | `#0D0F14` | Primary text; near-black base for the dark gate board |
| `--ink-secondary` | `#4B5563` | Secondary text, nav items, muted labels |
| `--ink-muted` | `#9CA3AF` | Tertiary text, column headers, placeholder copy |
| `--shell` | `#F5F3EE` | Main area background; dominant warm off-white ground |
| `--shell-dim` | `#EDEBE6` | Nav rail background; stat grid separator fill |
| `--shell-press` | `#E4E1D8` | Hover / active state on shell surfaces |
| `--navy` | `#1C2B4A` | Crown, brand strip, active nav indicator, cost stat color |
| `--navy-hover` | `#243661` | Navy interactive hover |
| `--amber` | `#F59B22` | Live and anomaly signal only — never decorative |
| `--amber-dim` | `rgba(245,155,34,0.10)` | Anomaly row background, amber badges at rest |
| `--amber-text` | `#B45309` | Amber text on light backgrounds |
| `--rule` | `rgba(13,15,20,0.09)` | Standard hairline separator |
| `--rule-strong` | `rgba(13,15,20,0.17)` | Borders on inputs, cards, table containers |
| `--white` | `#FFFFFF` | Input backgrounds, card fills |
| `--danger` | `#DC2626` | Error states, error row left-edge |
| `--danger-dim` | `rgba(220,38,38,0.08)` | Error row background, alert background |
| `--success` | `#16A34A` | Machine active dot, success alerts |
| `--success-dim` | `rgba(22,163,74,0.10)` | Success alert background |

### Amber discipline

Amber (`--amber`) appears only on: live-dot, anomaly-row left edge, anomaly status badge, crown stat `.is-live` value, and `.btn-amber` (an explicit call-out action). Any new surface that reaches for amber without a live-or-anomaly semantic is out of world.

---

## Typography

Two families; no others are introduced on any surface.

| Token | Stack |
|---|---|
| `--font-data` | `'IBM Plex Mono', ui-monospace, 'Cascadia Code', monospace` |
| `--font-ui` | `'Inter', system-ui, -apple-system, sans-serif` |

Both are loaded from Google Fonts at weights 400 / 450 / 500 / 550 / 600 / 700 (Inter) and 400 / 500 / 600 / 700 (IBM Plex Mono).

### Type ramp (as shipped)

| Role | Family | Size | Weight | Tracking | Transform |
|---|---|---|---|---|---|
| Body / base | `--font-ui` | 14px | 400 | — | — |
| Nav item | `--font-ui` | 13px | 450 / 550 active | — | — |
| Page header h1/h2 | `--font-ui` | 19px | 600 | −0.015em | — |
| Crown stat value | `--font-data` | 20px | 600 | −0.02em | — |
| Stat card value | `--font-data` | 26px | 600 | −0.03em | — |
| Stat card value (small) | `--font-data` | 13px | 600 | — | — |
| Section / board label | `--font-data` | 9px–10px | 600 | 0.14em | uppercase |
| Crown title / label | `--font-data` | 11px | 500 / 600 | 0.06–0.14em | uppercase |
| Crown stat label | `--font-data` | 9px | 400 | 0.12em | uppercase |
| Table header (th) | `--font-data` | 9px | 600 | 0.14em | uppercase |
| Table data cell | `--font-ui` | 13px | 400 | — | — |
| Table mono cell | `--font-data` | 12px | 400 | — | tnum |
| Status badge | `--font-data` | 9px | 700 | 0.08em | uppercase |
| Button label | `--font-data` | 10px | 700 | 0.10em | uppercase |
| Nav section label | `--font-data` | 9px | 600 | 0.14em | uppercase |
| Brand name | `--font-data` | 12px | 500 | 0.08em | uppercase |
| Footer | `--font-data` | 10px | — | 0.05em | — |

All numeric data in tabular contexts (cost, tokens, counts) carries `font-feature-settings: "tnum" 1` to prevent column-width jitter.

---

## Spacing Scale

Eight named steps; no half-steps are introduced in the build.

| Token | Value |
|---|---|
| `--s1` | 4px |
| `--s2` | 8px |
| `--s3` | 12px |
| `--s4` | 16px |
| `--s5` | 20px |
| `--s6` | 24px |
| `--s8` | 32px |
| `--s10` | 40px |

---

## Radii

The world runs on hairlines and 90° corners. Three minimal values; never pill-shaped on primary surfaces (exception: `.pill` utility uses 20px for tag chips only).

| Token | Value | Applied to |
|---|---|---|
| `--r0` | 2px | Status badges, inline `code`, focus ring |
| `--r1` | 3px | Buttons, inputs, cards, table containers, icon buttons |
| `--r2` | 4px | Defined; not observed in primary component use |

---

## Elevation

Shadow tokens exist for functional layering (popovers, the mobile nav drawer). They are not used as decorative surface treatment on cards or sections — depth is achieved through tonal contrast between `--shell` and `--navy` / `--ink`.

| Token | Value | Used on |
|---|---|---|
| `--shadow-sm` | `0 1px 3px rgba(13,15,20,0.07), 0 1px 2px rgba(13,15,20,0.04)` | Reserved / available |
| `--shadow-md` | `0 4px 12px rgba(13,15,20,0.09), 0 2px 4px rgba(13,15,20,0.05)` | Machine selector popover |
| `--shadow-panel` | `3px 0 20px rgba(13,15,20,0.14)` | Mobile nav drawer on-screen |

---

## Shell Layout

The full-viewport shell is a flex row: nav rail then main column. Main is a flex column: crown → optional machine topbar → scrollable container → footer.

```
┌─────────────────────────────────────────────┐
│  .sidebar-nav (204px / 56px collapsed)      │
│  ┌─────────────────────────────────────┐    │
│  │  .brand (navy, 52px)                │    │
│  │  .nav-menu (flex-1, scrollable)     │    │
│  │  .nav-user-card (pinned bottom)     │    │
│  └─────────────────────────────────────┘    │
│                                              │
│  .main-content (flex-1)                     │
│  ┌─────────────────────────────────────┐    │
│  │  .page-crown (navy, 52px)           │    │
│  │  .machine-topbar (shell-dim, admin) │    │
│  │  .container (flex-1, overflow-y)    │    │
│  │  footer (monospace, 10px)           │    │
│  └─────────────────────────────────────┘    │
└─────────────────────────────────────────────┘
```

### Nav rail

- Background: `--shell-dim`; border-right: `1px solid --rule-strong`
- Expanded: `--nav-width` (204px); collapsed: `--nav-collapsed` (56px)
- Collapse transition: `width 0.18s cubic-bezier(0.4,0,0.2,1)`
- Brand strip at top: `--navy` background, 52px height, aligns to crown
- Nav items: 36px height, `--font-ui` 13px, `border-right: 2px solid transparent`; active state sets `border-right-color: --navy`
- Active item background: `--shell-press`; active text: `--navy`, weight 550
- Section labels: `--font-data` 9px, uppercase, 0.14em tracking, `--ink-muted`
- User card: pinned bottom, 28px avatar circle in `--navy` with initial monogram

### Page crown

- Background: `--navy`; height: `--crown-h` (52px)
- Left: `crown-title` (muted monospace label) + `crown-route` (bright monospace page name)
- Right: `crown-stats` — live numeric readouts with amber `.is-live` modifier and dimmed `.is-zero` modifier
- Crown stat values: 20px IBM Plex Mono, weight 600, `tnum`
- Crown stat labels: 9px, 0.12em tracking, uppercase, `rgba(255,255,255,0.38)`
- Dividers: `1px × 18px`, `rgba(255,255,255,0.10)`
- Refresh button: 30px icon button, transparent on navy

### Machine topbar (admin only)

- Background: `--shell-dim`; border-bottom: `1px solid --rule`
- Shown below the crown; hidden for non-admin users (server-enforced scope)
- Contains: Team select (180px, 30px height) + Machine multi-select popover (280px dropdown)
- Machine panel uses `--shadow-md` and `--white` background

---

## Components

### Stat grid

- `display: grid`, `grid-template-columns: repeat(auto-fill, minmax(196px, 1fr))`
- `gap: 1px; background: --rule-strong` — the 1px gaps are the visible grid lines
- Each cell (`stat-card`): `--shell` background, padding `--s5 --s6`
- Card label (h3): `--font-data` 9px, uppercase, 0.14em tracking, `--ink-muted`
- Card value: `--font-data` 26px, weight 600, `tnum`, `--ink`
  - `.active` modifier: `--amber`
  - `.cost` modifier: `--navy`
  - `.small` modifier: 13px, line-height 1.35 (for date strings)

### Gate board (dark ground)

The primary departures-board artifact. Active sessions table renders on near-black ground so amber live badges carry authority without any glow.

- `.gate-board .table-container`: background `--ink`, border `rgba(255,255,255,0.08)`
- `thead`: `rgba(255,255,255,0.04)` background
- `th`: color `rgba(255,255,255,0.30)`, border `rgba(255,255,255,0.07)`
- `td`: color `rgba(255,255,255,0.82)`, border `rgba(255,255,255,0.055)`
- `td.mono / td.data`: color `rgba(255,255,255,0.75)`
- Row hover: `rgba(255,255,255,0.04)` background
- `.session-active` row: `rgba(28,43,74,0.55)` background; first-cell border-left: `2px solid --amber`
- `.anomaly` row: `rgba(245,155,34,0.12)` background; first-cell border-left: `2px solid --amber`
- Board title and section labels in dark context: `rgba(255,255,255,0.45)`

The standard (light) table is used outside the gate board. Its row states:
- `.session-active`: `rgba(28,43,74,0.038)` background; left-edge `2px solid --navy`
- `.anomaly`: `--amber-dim` background; left-edge `2px solid --amber`
- `.session-error`: `--danger-dim` background; left-edge `2px solid --danger`

Table structure:
- `th`: `--font-data` 9px, 600, 0.14em, uppercase; padding `--s3 --s4`; first child `--s5` left
- `td`: `--font-ui` 13px; padding `--s3 --s4`; `vertical-align: middle`
- `.num` columns: `text-align: right` (cost, tokens)

### Status badges

- Base: `inline-flex`, `--font-data` 9px, weight 700, 0.08em tracking, uppercase; `border-radius: --r0`; padding `1px --s2`
- `.active`: `--amber` background, `--ink` text
- `.anomaly`: `--amber` background, `--ink` text, `pulse-badge` animation (2s ease-in-out, 60% opacity at 50%)
- `.complete`: `rgba(13,15,20,0.08)` background, `--ink-secondary` text
- `.idle` / `.on-time`: no background, `--ink-muted` text
- `.error`: `--danger-dim` background, `--danger` text

Live dot: `6px × 6px` circle, `--amber`, same `pulse-badge` animation. Idle variant: `--ink-muted`, no animation.

### Buttons

Standard button: 34px height, `--r1` radius, `--font-data` 10px weight 700 uppercase 0.10em tracking.

| Variant | Background | Border | Text | Hover |
|---|---|---|---|---|
| Default | `--ink` | `--ink` | `--white` | `--navy` bg+border |
| `.btn-secondary` | transparent | `--ink` | `--ink` | `--shell-press` bg |
| `.btn-amber` | `--amber` | `--amber` | `--ink` | `#e08c1a` bg+border |
| `.btn-ghost` | transparent | transparent | `--ink-secondary` | `--shell-press` bg |
| `.btn-small` | — | — | — | height 28px, padding `0 --s3`, 9px font |

Disabled: `opacity: 0.38`, `pointer-events: none`. Focus: `2px solid --amber`, offset 2px.

Icon button (`.icon-btn`): 34px × 34px, `border: 1px solid --rule-strong`, `--r1`, `--ink-secondary`. On dark backgrounds (`.mobile-topbar`): border `rgba(255,255,255,0.15)`, text `rgba(255,255,255,0.70)`.

### Form inputs

- Height: 34px; background: `--white`; border: `1px solid --rule-strong`; radius: `--r1`; font: `--font-ui` 13px
- Focus: border `--navy`; box-shadow `0 0 0 3px rgba(28,43,74,0.10)`, no outline
- Caret: `--navy`
- Select: custom SVG chevron via background-image; padding-right `--s8`
- Labels: `--font-ui` 12px, weight 500, `--ink-secondary`
- Checkboxes / radios: `accent-color: --navy`

### Cards

Generic card: `--white` background, `border: 1px solid --rule-strong`, `--r1`, padding `--s6`.

### Alert banners

- Base: `display: flex`, padding `--s4`, `border-radius: --r1`, border `1px solid`
- `.alert-warning`: `--amber-dim` bg, `rgba(245,155,34,0.25)` border, `--amber-text` text
- `.alert-error`: `--danger-dim` bg, `rgba(220,38,38,0.22)` border, `--danger` text
- `.alert-success`: `--success-dim` bg, `rgba(22,163,74,0.22)` border, `--success` text

### Pills / tags

`border-radius: 20px` — the one fully rounded element in the system; used only for tagging / pill chips. Base: `rgba(13,15,20,0.07)` bg, `--ink-secondary`. Variants: `.amber`, `.navy`, `.success`, `.danger`.

---

## Responsive Breakpoints

| Breakpoint | Change |
|---|---|
| `≤ 1024px` | Mobile mode: `.mobile-topbar` shown (navy, 52px); sidebar becomes off-canvas drawer (`position: fixed`, `translateX(-100%)`); `.open` class slides it in; backdrop shown behind |
| `≤ 768px` | Container padding drops to `--s4` (16px); stats-grid forced to 2-col; crown padding tightened; crown stat value drops to 16px |
| `≤ 480px` | Crown stats hidden entirely; stats-grid 1×1fr |

Mobile topbar: `--navy` background, 52px height, hamburger icon-btn + mobile brand wordmark.

---

## Icon system

Material Icons ligature font (Google CDN), loaded as `family=Material+Icons`. All icons via `<i class="material-icons">ligature_name</i>`. Icon size: 18px in nav, 16px in crown/footer controls, 15px in buttons, 14–16px in badges and alerts.

---

## Focus and selection

- Global focus ring: `2px solid --amber`, offset 2px, `border-radius: --r0`
- Text selection: `rgba(28,43,74,0.16)` background, `--ink` text
- Scrollbars: 5px track, `--rule-strong` thumb, `--ink-muted` on hover

---

## Rules

1. **Amber is an alert signal, not a brand accent.** Use only for live session indicators, anomaly states, and explicit amber call-to-action buttons. Never apply to section headings, charts, or decorative flourishes.
2. **All numeric data in columns uses `tnum`.** Any tabular number (cost, tokens, counts, timestamps) carries `font-feature-settings: "tnum" 1` to prevent layout shift.
3. **Shadows are functional, not decorative.** `--shadow-md` and `--shadow-panel` appear only on layered surfaces (popovers, the mobile nav drawer). Cards, sections, and stat grids use tonal contrast only.
4. **Left-edge accent on rows, not full-row color alone.** State rows (active, anomaly, error) combine a `2px solid` left border with a low-opacity background fill. Neither element alone is sufficient.
5. **IBM Plex Mono carries data authority.** All session IDs, cost figures, token counts, timestamps, durations, labels in the crown, and button labels use `--font-data`. Inter carries navigation, page headings, and body prose.
6. **90° corners on all structural surfaces.** `--r1` (3px) is the system cap for interactive controls. The sole exception is `.pill` (tag chip, 20px) which is an explicit tag-chip primitive.
7. **Hairline separators, not space, divide regions.** `1px solid --rule` or `--rule-strong` separates every structural boundary. Whitespace alone is insufficient for the SOLARI world.

---

## Not canonized

- **Brand mark gradient** (`#fbbf24 → #818cf8 → #4338ca`): The WhirlpoolBee logomark SVG uses a gradient outside the system palette. This is a locked identity artifact, not a surface-treatment rule. The gradient must not be applied to any UI surface other than the 32×32 mark.
- **`--ink` warm/cool drift**: The direction contract specified a warm near-black (`#1A1814`). The build ships a cooler near-black (`#0D0F14`). The cooler value is canonical per the build; the contract divergence is noted but not a defect to repair.
- **Emoji in notification button** (`🔔 🔕`): Runtime JavaScript strings in the notification permission button. Not a designed element; not a system rule for future surfaces.
