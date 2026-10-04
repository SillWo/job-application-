# Ui — Style Reference
> clinical blueprint on frosted paper

**Theme:** light

shadcn/ui is a monochromatic design-system workshop: pure white canvas, soft warm-gray surfaces, and large-radius cards floating on hairline borders. The base interface stays neutral — black text, white surfaces, and gray secondary tones. Muted semantic colors appear only on meaningful status and feedback surfaces; they do not serve as branding or decoration. Destructive actions keep their dedicated red. Typography uses Inter with restrained tracking, creating a quiet, code-adjacent feel that reads as developer infrastructure rather than consumer product.

## Tokens — Colors

| Name | Value | Token | Role |
|------|-------|-------|------|
| Canvas | `#f5f5f5` | `--color-canvas` | Page background, muted surface fills, secondary buttons |
| Paper | `#ffffff` | `--color-paper` | Card surfaces, popover backgrounds, primary button fills |
| Surface Alt | `#fafafa` | `--color-surface-alt` | Subtle navigation surface, card variant, input resting state |
| Ink | `#0a0a0a` | `--color-ink` | Primary text, headings, button labels, icon strokes |
| Ink Soft | `#171717` | `--color-ink-soft` | Filled button backgrounds, secondary text on light surfaces |
| Mid Gray | `#737373` | `--color-mid-gray` | Muted body text, placeholder text, helper labels, icon fills at rest |
| Hairline | `#e5e5e5` | `--color-hairline` | Borders, input outlines, card edges, badge outlines |
| Ember | `#e7000b` | `--color-ember` | Dedicated red for destructive actions |

### Semantic Status Palette

Use these muted colors only for meaningful status and feedback surfaces, such as status badges, indicators, and feedback panels. Keep the base interface neutral and do not use semantic colors for branding or decoration. Every status must include a visible text label; icons can reinforce meaning when present, and color alone must never convey status. Use the same status palette at all breakpoints without changing layout. Unknown, not configured, and cancelled states with no result use a neutral gray treatment.

| Meaning | Text | Background | Border | Indicator | Tokens |
|---------|------|------------|--------|-----------|--------|
| Danger: failed or rejected | `#9f3a38` | `#f8ebea` | `#e8c5c2` | `#c87972` | `--status-danger-text`, `--status-danger-bg`, `--status-danger-border`, `--status-danger-indicator` |
| Warning: ongoing or processing | `#85631c` | `#faf3df` | `#e6d6a6` | `#b99545` | `--status-warning-text`, `--status-warning-bg`, `--status-warning-border`, `--status-warning-indicator` |
| Success: positive result | `#35694b` | `#edf5ef` | `#c2d9c8` | `#79a68a` | `--status-success-text`, `--status-success-bg`, `--status-success-border`, `--status-success-indicator` |
| Info: paused or waiting for user | `#3b6087` | `#edf2f8` | `#c6d5e6` | `#7f9cbe` | `--status-info-text`, `--status-info-bg`, `--status-info-border`, `--status-info-indicator` |
| Neutral: unknown, not configured, cancelled with no result | Existing neutral badge text token (`#404040`) | `#f5f5f5` | `#e5e5e5` | `#a3a3a3` | Existing neutral palette tokens |

### Canonical Status Mapping

| Area | Statuses | Treatment |
|------|----------|-----------|
| Sessions | `CREATED`, `PREPARING`, `RUNNING`, `STOPPING` | Warning (ongoing) |
| Sessions | `PAUSED`, `STOPPED` | Info (paused) |
| Sessions | `COMPLETED` | Success |
| Sessions | `FAILED` | Danger |
| Sessions | `CANCELLED` | Neutral |
| Vacancies | `PROCESSING` | Warning (ongoing) |
| Vacancies | `SUCCESS`, `SUBMITTED`, `ALREADY_APPLIED`, `REPORTED` | Success |
| Vacancies | `REJECTED`, `ERROR` | Danger |
| Vacancies | `CANCELLED` | Neutral |
| Resume | Checking | Warning (ongoing) |
| Resume | Valid, changed | Success |
| Resume | Unavailable | Danger |
| Resume | Unconfigured | Neutral |
| Resume completion | `empty`, `complete`, `partial`, `error` | Neutral, success, warning, danger |
| Model | `LOADING`, `PENDING` | Warning (ongoing) |
| Model | Available, connected, healthy | Success |
| Model | Unavailable, disconnected, unhealthy, status error | Danger |
| Model | Unknown | Neutral |
| User action | Waiting for user | Info (paused) |
| Work | Active processing | Warning (ongoing) |

### Status Tokens

| Token | Value |
|-------|-------|
| `--status-danger-text` | `#9f3a38` |
| `--status-danger-bg` | `#f8ebea` |
| `--status-danger-border` | `#e8c5c2` |
| `--status-danger-indicator` | `#c87972` |
| `--status-warning-text` | `#85631c` |
| `--status-warning-bg` | `#faf3df` |
| `--status-warning-border` | `#e6d6a6` |
| `--status-warning-indicator` | `#b99545` |
| `--status-success-text` | `#35694b` |
| `--status-success-bg` | `#edf5ef` |
| `--status-success-border` | `#c2d9c8` |
| `--status-success-indicator` | `#79a68a` |
| `--status-info-text` | `#3b6087` |
| `--status-info-bg` | `#edf2f8` |
| `--status-info-border` | `#c6d5e6` |
| `--status-info-indicator` | `#7f9cbe` |

The `--color-ember` value (`#e7000b`) remains reserved for destructive actions. Status errors use the muted danger palette above.

## Tokens — Typography

### Inter — All interface text — body at 14px/400, headings ranging 24–48px/600, buttons at 13–14px/500. Inter's neutral letterforms keep dense workflow screens legible; weight 600 at 48px with -0.05em tracking produces compact display headlines. · `--font-inter`
- **Substitute:** system-ui
- **Weights:** 400, 500, 600
- **Sizes:** 12, 13, 14, 16, 18, 24, 30, 36, 48
- **Line height:** 1.10, 1.11, 1.20, 1.33, 1.43, 1.50, 1.56, 1.63, 2.00
- **Letter spacing:** -0.0500em at display (48px), -0.0250em at subheading (24–30px), 0.0500em at caption (12px uppercase). Tracking tightens aggressively at large sizes and loosens slightly at small uppercase labels.
- **OpenType features:** `"ss01" on, "cv11" on`
- **Role:** All interface text — body at 14px/400, headings ranging 24–48px/600, buttons at 13–14px/500.

### Type Scale

| Role | Size | Line Height | Letter Spacing | Token |
|------|------|-------------|----------------|-------|
| caption | 12px | 1.33 | 0.6px | `--text-caption` |
| body | 14px | 1.43 | — | `--text-body` |
| body-lg | 16px | 1.5 | — | `--text-body-lg` |
| subheading | 18px | 1.56 | — | `--text-subheading` |
| heading-sm | 24px | 1.33 | -0.6px | `--text-heading-sm` |
| heading | 30px | 1.2 | -0.75px | `--text-heading` |
| heading-lg | 36px | 1.11 | -0.9px | `--text-heading-lg` |
| display | 48px | 1.1 | -2.4px | `--text-display` |

## Tokens — Spacing & Shapes

**Base unit:** 4px

**Density:** compact

### Spacing Scale

| Name | Value | Token |
|------|-------|-------|
| 4 | 4px | `--spacing-4` |
| 8 | 8px | `--spacing-8` |
| 12 | 12px | `--spacing-12` |
| 16 | 16px | `--spacing-16` |
| 20 | 20px | `--spacing-20` |
| 24 | 24px | `--spacing-24` |
| 48 | 48px | `--spacing-48` |

### Border Radius

| Element | Value |
|---------|-------|
| cards | 24px |
| small controls | 18px |
| badges | 18px |
| inputs | 18px |
| nested surfaces | 18px |
| buttons | 18px |

### Shadows

| Name | Value | Token |
|------|-------|-------|
| subtle | `oklab(0.145 -0.00000143796 0.00000340492 / 0.05) 0px 0px ...` | `--shadow-subtle` |
| subtle-2 | `lab(2.75381 0 0) 0px 0px 0px 0px` | `--shadow-subtle-2` |

### Layout

- **Page max-width:** 1280px
- **Section gap:** 48-80px
- **Card padding:** 20px
- **Element gap:** 8px
- **Navigation:** sticky full-width top bar with centered inner content
- **Main:** centered page on the canvas; cards span the page content width
- **Forms:** full-width cards with a centered, left-aligned form content rail

### Form Widths

Forms use full-width cards with a left-aligned content rail. Apply the rail or
field utility to the wrapper (not only its input) so labels, fieldsets,
subsections, tag lists, and controls share the same readable measure. Choose a
semantic size for the expected content: compact (320px), medium (480px), wide
(720px), or prose (760px), within the 880px form rail. On mobile each utility
becomes width 100% so controls remain within the viewport.

| Name | Value | Token |
|------|-------|-------|
| Form content max | 880px | `--form-content-max` |
| Compact field | 320px | `--field-compact` |
| Medium field | 480px | `--field-medium` |
| Wide field | 720px | `--field-wide` |
| Prose field | 760px | `--field-prose` |

## Components

### Primary Filled Button
**Role:** High-emphasis action (Submit, Save, Create)

Background #0a0a0a, text #fafafa, border none, radius 18px, padding 0px 12px (compact) or 8px 16px (comfortable), font 14px Inter weight 500. Height ≈ 36–40px. The dark-on-light inversion is the primary action treatment; the fully rounded radius (18px on a ~36px height) produces perfect pill geometry.

### Secondary Ghost Button
**Role:** Low-emphasis action (Cancel, Back)

Background #f5f5f5, text #0a0a0a, no border, radius 18px, padding 0px 12px or 8px 16px, font 14px weight 500. Soft gray fill reads as a tonal sibling to the primary rather than a muted alternative — both buttons share shape and type, differing only in lightness.

### Outline Button
**Role:** Tertiary action with visible boundary

Background transparent, text #0a0a0a, border 1px solid #e5e5e5, radius 18px, padding 0px 12px or 8px 10px. The hairline border defines the shape without weight — preferred when the button sits inside a card or alongside filled controls.

### Card
**Role:** Content container for blocks, previews, dashboard panels

Background #ffffff, radius 24px, border 1px solid #e5e5e5, shadow oklab(0.145/.05) 0 0 0 1px + rgba(0,0,0,0.1) 0 1px 3px + rgba(0,0,0,0.1) 0 1px 2px -1px, padding 20px. The 1px hairline shadow stacks with a faint elevation layer — cards sit visually raised but remain flat and understated.

### Nested Card Header/Footer
**Role:** Header or footer strip inside a card

Asymmetric radius — top corners 24px on header, bottom corners 24px on footer. Padding 20px horizontal, transparent fill. Provides a subtle tonal band within card boundaries without introducing a new color.

### Input Field
**Role:** Text entry, search, form controls

Background #f5f5f5 (resting) or transparent (inline), text #0a0a0a, and a persistent 1px #e5e5e5 hairline for editable resume fields. Hover may strengthen the hairline; focus uses the ink border and the global focus ring. Radius 18px, padding 8px 10px, font 14px weight 400. The boundary keeps controls legible against card surfaces without introducing color.

### Multi-select Field
**Role:** Compact selection of multiple resume options

The collapsed trigger is a paper surface with a 1px hairline, a muted label, selected values, and an ink chevron. The opened popover uses the paper surface, hairline, and subtle shadow; options are stacked as full-width 48px rows with aligned 18px checkboxes. Selected rows use only a neutral surface shift and stronger ink border. The full-width `Выбрать` action closes the popover. Keep this component achromatic and preserve the underlying enum array in the API payload.

### Resume Experience Card
**Role:** Repeated work-experience entry

Use a paper surface with a 1px hairline, 18px nested radius, and a separated header row. Individual controls retain their own persistent boundaries so adjacent fields cannot visually merge.

### Badge — Solid
**Role:** Tag, status pill, counter

Background #171717, text #fafafa, radius 18px, padding 2px 8px, font 12px weight 500. Pill-shaped at 18px radius — the minimum height creates a capsule tag.

### Badge — Soft
**Role:** Neutral label, category tag

Background #f5f5f5, text #171717, radius 18px, padding 2px 8px, font 12px weight 500. Same capsule geometry as solid badge, tonal variant.

### Badge — Outline
**Role:** Subtle tag with no fill

Transparent background, text #0a0a0a, radius 18px, padding 2px 8px. The lightest-weight tag — used when the label is informational rather than categorical.

### Top Navigation Surface
**Role:** Sticky top navigation bar

Background #ffffff with slight transparency, full-width, and a hairline bottom border. It sits one tonal step above the canvas (#f5f5f5), keeping navigation distinct while the centered page remains the primary focus.

### Breadcrumb Trail
**Role:** Hierarchical path indicator

Inline text with chevron separators, font 14px weight 400, color #737373 for separators and #0a0a0a for the current segment. No background, no borders — purely typographic hierarchy.

### Stat Block
**Role:** Large numeric metric display

Label in 12–14px uppercase #737373, value in 30–48px weight 600 #0a0a0a with tight tracking. Progress bar or comparison text in 14px #737373. The block relies on typographic scale alone — no card chrome — to establish the metric.

### Search Trigger
**Role:** Command palette / search input

Background #f5f5f5, text #737373, radius 18px, padding 8px 10px, with a keyboard shortcut indicator (e.g., ⌘K) right-aligned. Functions as both a button and an input affordance.

### Destructive Action
**Role:** Delete, remove, revoke — error-adjacent interactions

Text or icon in #e7000b for destructive actions. Keep this dedicated red separate from status feedback: failed and rejected statuses use the muted semantic danger palette. Neither treatment is decorative.

## Do's and Don'ts

### Do
- Use #0a0a0a on #ffffff for filled buttons — the dark inversion is the only primary action treatment.
- Maintain 18px radius on all buttons, inputs, and badges for perfect pill geometry; use 24px radius only on cards.
- Set display headlines at 48px/600 with -0.0500em tracking — Inter's neutral weight at this size with aggressive tightening produces the engineered headline voice.
- Reserve #e7000b exclusively for destructive actions; never use it for status errors, decoration, branding, or non-error emphasis. Use muted semantic status colors for status feedback.
- Stack card shadows as 1px hairline + 1px + 2px offset — the combined effect is a barely-perceptible elevation that reads as 'card' without drama.
- Use #f5f5f5 for secondary surfaces and inputs; use #fafafa for subtle navigation and card variants — the three-tone surface stack (canvas → soft → paper) creates layering without borders.

### Don't
- Do not introduce brand or decorative colors. Keep the base interface neutral; use only the documented muted semantic palette on meaningful status and feedback surfaces, and reserve #e7000b for destructive actions.
- Do not use border-radius values other than 18px (interactive) or 24px (containers); avoid square corners on any element.
- Do not skip the 1px hairline border on cards — the shadow alone does not define the card edge in this system.
- Do not set body text below 14px or above #737373 lightness — the type scale is deliberately compact.
- Do not apply gradients, colored shadows, or decorative accent fills — every surface is a solid tone. Semantic status fills are limited to meaningful status and feedback surfaces.
- Do not use letter-spacing wider than 0.05em or tighter than -0.05em; tracking outside this range breaks the typographic system.
- Do not mix filled and outline buttons of the same size in a single row without visual rhythm — alternate ghost or secondary variants.

## Surfaces

| Level | Name | Value | Purpose |
|-------|------|-------|---------|
| 0 | Canvas | `#f5f5f5` | Page background, broadest layer |
| 1 | Top navigation | `#fafafa` | Navigation surface, one step lighter than canvas |
| 2 | Card | `#ffffff` | Primary content container, brightest surface |
| 3 | Input Fill | `#f5f5f5` | Resting input field, matches canvas tone for subtle differentiation |

## Elevation

- **Card:** `0 0 0 1px rgba(23,23,23,0.05), 0 1px 3px rgba(0,0,0,0.1), 0 1px 2px -1px rgba(0,0,0,0.1)`
- **Button (filled):** `none — relies on tonal contrast, not shadow`
- **Input (focus):** `1px solid #e5e5e5 ring, no offset shadow`

## Imagery

Minimal imagery — the system is almost entirely UI. No hero photography, no illustrations, no decorative graphics. Product showcases are rendered as component mockups (cards, inputs, buttons) in a grid, serving as both documentation and visual content. Icons are thin-stroke geometric marks (likely Lucide-derived) at 1.5–2px stroke weight in #0a0a0a or #737373, used sparingly as functional cues. The visual language IS the UI components themselves — the page functions as a living style guide where every visible element is a design token made visible.

## Agent Prompt Guide

**Quick Color Reference**
- Canvas/background: #f5f5f5
- Card/surface: #ffffff
- Primary text: #0a0a0a
- Muted text: #737373
- Border: #e5e5e5
- primary action: #171717 (filled action)
- Destructive action: #e7000b
- Status palette: use the canonical semantic status tokens above for feedback surfaces only

**Example Component Prompts**
1. Create a dashboard stat card: white (#ffffff) background, 24px radius, 1px solid #e5e5e5 border, shadow 0 0 0 1px rgba(23,23,23,0.05) + 0 1px 3px rgba(0,0,0,0.1) + 0 1px 2px -1px rgba(0,0,0,0.1), 20px padding. Label in 12px uppercase #737373, value in 36px Inter weight 600 #0a0a0a with -0.025em tracking.

2. Create a filled dark button: background #0a0a0a, text #fafafa, no border, 18px radius, padding 0px 12px, font 14px Inter weight 500. Height 36px. No shadow — tonal contrast only.

3. Create a ghost secondary button: background #f5f5f5, text #0a0a0a, no border, 18px radius, padding 0px 12px, font 14px weight 500. Same dimensions as the filled button for visual parity.

4. Create an input field: background #f5f5f5, text #0a0a0a, placeholder #737373, no border at rest, 18px radius, padding 8px 10px, font 14px weight 400. On focus: 1px solid #e5e5e5 ring with no offset.

5. Create a neutral badge tag: background #171717, text #fafafa, 18px radius (full pill), padding 2px 8px, font 12px Inter weight 500. For a status badge, use its documented semantic status tokens and include a visible text label; an icon can reinforce meaning when present.

## Design Philosophy

shadcn/ui is built on three principles visible in every token: (1) neutral by default — muted semantic colors communicate meaningful status and feedback only; (2) radius defines hierarchy — 18px for interactive elements, 24px for containers, never anything in between; (3) elevation is whisper-quiet — the card shadow is barely perceptible, relying on 1px hairlines and tonal contrast rather than dramatic drop shadows. The system is designed to be copied, modified, and owned — every value is explicit, every token is simple, and nothing is locked behind abstraction.

## Similar Brands

- **Vercel** — Same monochromatic palette, same Inter sans pairing, same pill-shaped buttons with tight letter-spacing on display text
- **Linear** — Identical approach to neutral UI with restrained status feedback, tight typographic tracking, and hairline-bordered cards
- **Radix UI** — Same developer-tool visual language — neutral surfaces, geometric type, and component-first documentation layout
- **Tailwind UI** — Matching restrained palette, identical border-radius scale (large radii on containers), and code-adjacent minimal chrome
- **Cal.com** — Same compact density, same pill-badge system, and the same neutral-first approach

## Quick Start

### CSS Custom Properties

```css
:root {
  /* Colors */
  --color-canvas: #f5f5f5;
  --color-paper: #ffffff;
  --color-surface-alt: #fafafa;
  --color-ink: #0a0a0a;
  --color-ink-soft: #171717;
  --color-mid-gray: #737373;
  --color-hairline: #e5e5e5;
  --color-ember: #e7000b;

  /* Semantic status colors — use only on meaningful status/feedback surfaces */
  --status-danger-text: #9f3a38;
  --status-danger-bg: #f8ebea;
  --status-danger-border: #e8c5c2;
  --status-danger-indicator: #c87972;
  --status-warning-text: #85631c;
  --status-warning-bg: #faf3df;
  --status-warning-border: #e6d6a6;
  --status-warning-indicator: #b99545;
  --status-success-text: #35694b;
  --status-success-bg: #edf5ef;
  --status-success-border: #c2d9c8;
  --status-success-indicator: #79a68a;
  --status-info-text: #3b6087;
  --status-info-bg: #edf2f8;
  --status-info-border: #c6d5e6;
  --status-info-indicator: #7f9cbe;

  /* Typography — Font Families */
  --font-inter: 'Inter', ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;

  /* Typography — Scale */
  --text-caption: 12px;
  --leading-caption: 1.33;
  --tracking-caption: 0.6px;
  --text-body: 14px;
  --leading-body: 1.43;
  --text-body-lg: 16px;
  --leading-body-lg: 1.5;
  --text-subheading: 18px;
  --leading-subheading: 1.56;
  --text-heading-sm: 24px;
  --leading-heading-sm: 1.33;
  --tracking-heading-sm: -0.6px;
  --text-heading: 30px;
  --leading-heading: 1.2;
  --tracking-heading: -0.75px;
  --text-heading-lg: 36px;
  --leading-heading-lg: 1.11;
  --tracking-heading-lg: -0.9px;
  --text-display: 48px;
  --leading-display: 1.1;
  --tracking-display: -2.4px;

  /* Typography — Weights */
  --font-weight-regular: 400;
  --font-weight-medium: 500;
  --font-weight-semibold: 600;

  /* Spacing */
  --spacing-unit: 4px;
  --spacing-4: 4px;
  --spacing-8: 8px;
  --spacing-12: 12px;
  --spacing-16: 16px;
  --spacing-20: 20px;
  --spacing-24: 24px;
  --spacing-48: 48px;

  /* Layout */
  --page-max-width: 1280px;
  --section-gap: 48-80px;
  --card-padding: 20px;
  --element-gap: 8px;
  --form-content-max: 880px;
  --field-compact: 320px;
  --field-medium: 480px;
  --field-wide: 720px;
  --field-prose: 760px;

  /* Border Radius */
  --radius-md: 18px;
  --radius-lg: 18px;
  --radius-xl: 18px;
  --radius-2xl: 18px;
  --radius-3xl: 24px;

  /* Named Radii */
  --radius-cards: 24px;
  --radius-small: 18px;
  --radius-badges: 18px;
  --radius-inputs: 18px;
  --radius-nested: 18px;
  --radius-buttons: 18px;

  /* Shadows */
  --shadow-subtle: oklab(0.145 -0.00000143796 0.00000340492 / 0.05) 0px 0px 0px 1px, rgba(0, 0, 0, 0.1) 0px 1px 3px 0px, rgba(0, 0, 0, 0.1) 0px 1px 2px -1px;
  --shadow-subtle-2: lab(2.75381 0 0) 0px 0px 0px 0px;

  /* Surfaces */
  --surface-canvas: #f5f5f5;
  --surface-navigation: #fafafa;
  --surface-card: #ffffff;
  --surface-input-fill: #f5f5f5;
}
```

### Tailwind v4

```css
@theme {
  /* Colors */
  --color-canvas: #f5f5f5;
  --color-paper: #ffffff;
  --color-surface-alt: #fafafa;
  --color-ink: #0a0a0a;
  --color-ink-soft: #171717;
  --color-mid-gray: #737373;
  --color-hairline: #e5e5e5;
  --color-ember: #e7000b;

  /* Semantic status colors — use only on meaningful status/feedback surfaces */
  --status-danger-text: #9f3a38;
  --status-danger-bg: #f8ebea;
  --status-danger-border: #e8c5c2;
  --status-danger-indicator: #c87972;
  --status-warning-text: #85631c;
  --status-warning-bg: #faf3df;
  --status-warning-border: #e6d6a6;
  --status-warning-indicator: #b99545;
  --status-success-text: #35694b;
  --status-success-bg: #edf5ef;
  --status-success-border: #c2d9c8;
  --status-success-indicator: #79a68a;
  --status-info-text: #3b6087;
  --status-info-bg: #edf2f8;
  --status-info-border: #c6d5e6;
  --status-info-indicator: #7f9cbe;

  /* Typography */
  --font-inter: 'Inter', ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;

  /* Typography — Scale */
  --text-caption: 12px;
  --leading-caption: 1.33;
  --tracking-caption: 0.6px;
  --text-body: 14px;
  --leading-body: 1.43;
  --text-body-lg: 16px;
  --leading-body-lg: 1.5;
  --text-subheading: 18px;
  --leading-subheading: 1.56;
  --text-heading-sm: 24px;
  --leading-heading-sm: 1.33;
  --tracking-heading-sm: -0.6px;
  --text-heading: 30px;
  --leading-heading: 1.2;
  --tracking-heading: -0.75px;
  --text-heading-lg: 36px;
  --leading-heading-lg: 1.11;
  --tracking-heading-lg: -0.9px;
  --text-display: 48px;
  --leading-display: 1.1;
  --tracking-display: -2.4px;

  /* Spacing */
  --spacing-4: 4px;
  --spacing-8: 8px;
  --spacing-12: 12px;
  --spacing-16: 16px;
  --spacing-20: 20px;
  --spacing-24: 24px;
  --spacing-48: 48px;

  /* Border Radius */
  --radius-md: 18px;
  --radius-lg: 18px;
  --radius-xl: 18px;
  --radius-2xl: 18px;
  --radius-3xl: 24px;

  /* Shadows */
  --shadow-subtle: oklab(0.145 -0.00000143796 0.00000340492 / 0.05) 0px 0px 0px 1px, rgba(0, 0, 0, 0.1) 0px 1px 3px 0px, rgba(0, 0, 0, 0.1) 0px 1px 2px -1px;
  --shadow-subtle-2: lab(2.75381 0 0) 0px 0px 0px 0px;
}
```
