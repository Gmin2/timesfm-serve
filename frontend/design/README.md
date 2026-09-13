# design system

reference for the dashboard ui. the screenshots in `inspo/` are the source, colors
below were sampled from their pixels, so treat them as close, not exact.

| file | what to take from it |
| --- | --- |
| `01-model-detail.png` | detail page: back link, logo tile, title + badge, id box with copy, code tabs, spec table |
| `02-api-keys-table.png` | list page: toolbar (search, filter, docs, primary), dense table, segmented usage bar, pagination footer |
| `03-create-key-drawer.png` | right side sheet with a step rail on the left, review table, code preview, sticky footer actions |
| `04-composer-tools-menu.png` | input card with a tinted footer strip, menu section label + icon rows |
| `05-model-picker-tooltip.png` | popover menu with hover row, dark info tooltip with dash meters |
| `06-sidebar-nav.png` | sidebar at zoom: workspace button, project select, nav rows with kbd hints |
| `07-workspace-switcher.png` | dropdown + side panel, divider sections, blue text action |
| `08-getting-started-card.png` | checklist card with progress ring, dark feature card with a blue cta |
| `09-campaign-dashboard.png` | kpi tabs over a line chart, filter pills, tabs with count chips, status pills |

## layout

- the app sits in a framed shell: gray page (`#ededed` or white), 1px border, ~12px
  radius, the frame clips the sidebar and main area
- sidebar ~300px at 2x (so ~232px real), warm bg, 1px right border
- topbar ~56px real height with breadcrumb `Icon Section / Page`, 1px bottom border
- detail pages use a centered column ~840px wide, list pages go full width
- page footer bars (pagination) use the sidebar tint with a top border

## color

neutrals are warm, never green or blue tinted.

| token | value | use |
| --- | --- | --- |
| `--background` | `#ffffff` | main surface |
| `--sidebar` | `#fcfbf9` | sidebar, code blocks, drawer rail, footer bars |
| `--muted` | `#f2f1ed` | active nav row, hover rows, chips, secondary buttons |
| `--track` | `#e3e2de` | empty meter segments |
| `--border` | `#ebeae7` | hairlines (cards, table rows, topbar) |
| `--border-strong` | `#e0dfdb` | inputs, outline buttons |
| `--foreground` | `#16151b` | titles, primary text, dark buttons |
| `--text` | `#3f3e3c` | body, nav rows, table cells |
| `--muted-foreground` | `#7a7976` | section labels, kbd hints, descriptions |
| `--primary` | `#386aff` | primary cta, focus, active page chip, links, meter fill |
| `--primary-soft` | `#eff6ff` | info badge bg (text `#1d5ae0`) |
| `--ink-card` | `#16151b` | dark feature card, tooltip is `#1a1a1a` |

status pills (bg / text), no border, 4px radius:

| state | bg | text |
| --- | --- | --- |
| active / good | `#dcfce7` | `#15803d` |
| paused / warning | `#ffedd5` | `#c2410c` |
| expired | `#ffedd5` | `#9a3412` |
| ended / error | `#fee2e2` | `#dc2626` |
| draft / info | `#dbeafe` | `#1d4ed8` |

deltas: up `#16a34a`, down `#dc2626`, both small and semibold.
meters: blue `#386aff` under ~70%, orange `#ff6802` near limit, red `#fd2c37` at limit.
chart line `#0063e6`, comparison line same hue dotted and lighter.

## type

- inter, 13-14px base, weight 400 body / 500 nav + titles / 600 only for big numbers
- page title 20px medium, section heading 15px medium, both `--foreground`
- section labels in the sidebar ("Build", "Manage") 12px `--muted-foreground`, no caps
- mono (ids, keys, code) 12.5px, `--text`, in a `--sidebar` box with a border
- kpi values 18px regular, tabular nums

## components

- **button primary** blue fill, white text, 6px radius, 32px tall
- **button dark** `--foreground` fill, white text (used for the one main action on detail pages)
- **button outline** white, 1px `--border-strong`, `--text`, optional trailing icon
- **nav row** 32px tall, 8px x padding, 16px icon at `--muted-foreground`, active row
  gets `--muted` bg with no border, sub items are indented under a 1px guide line
- **kbd** 18px square, 1px border, 11px `--muted-foreground`, sits at the row end
- **select trigger** white, 1px border, subtle shadow `0 1px 2px #0000000a`, up/down chevron
- **card** white, 1px `--border`, 10px radius, no shadow; nested cards get a 4px inner gap
- **table** header row plain white, 13px `--text`, rows 1px `--border`, no zebra,
  checkbox column first, cells vertically centered
- **chip** (permission, count) 1px border or `--muted` fill, 12px, 4px radius
- **segmented meter** 20 thin bars with 2px gaps, filled bars colored by threshold
- **tabs** text tabs with a 2px `--foreground` underline on the active one, optional count chip
- **kpi tabs** a row of stat cells over the chart; active cell gets the underline
- **menu / popover** white, 1px border, 12px radius, shadow `0 8px 24px #00000014`,
  section label in `--muted-foreground`, hover row `--muted` with 8px radius
- **tooltip** `#1a1a1a`, white title, `#a3a3a3` body, divider `#2a2a2a`
- **sheet** right side, sits inside the frame with 12px inset, two columns
  (step rail on `--sidebar`, content on white), footer with cancel text button + primary
- **step rail** check (green filled), current (blue dot), upcoming (dashed ring)
- **progress ring** thin blue arc on a light blue track, used for "getting started"

## icons

outline style, 1.5px stroke, 16px in rows, `--muted-foreground`. use the local
nucleo set, not lucide. brand marks (github, ecmwf, noaa) keep their own colors.

## motion

quiet. 150ms color transitions on hover, 180ms fade for route changes, sheets slide
in from the right over ~220ms. no motion on plotted values.
