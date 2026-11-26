# Themes

A theme changes how the **public page** looks: colours, fonts, corner radius and any extra
CSS you like. Each event picks its own theme (*Admin → Settings → Appearance*). The admin page
always uses the default look.

Two themes come with the app:

| Folder | Name | Look |
|---|---|---|
| `default` | Default | Blue and violet, the standard look |
| `retro` | Retro terminal | Green phosphor console with monospace text, square corners and scanlines. Light mode looks like green-bar printer paper. |

## Where themes live

- Built-in themes: `lanparty/themes/<id>/`
- Your own themes: `instance/themes/<id>/`, or the folder set in the `THEMES_DIR` environment
  variable (a relative path counts from the directory the app is started in). This folder
  survives app updates. Create it if it doesn't exist.

The folder name is the theme's id. It may use lowercase letters, digits, `-` and `_` (40 characters
at most). A custom theme can't use the name of a built-in one.

Themes are read from disk on every request, so new or changed themes show up without a restart.

## What a theme folder contains

```
instance/themes/neon/
├── theme.css     required
├── theme.json    optional: name and description shown to admins
└── font.woff2    optional: fonts and images used by theme.css
```

`theme.json`:

```json
{
  "name": "Neon",
  "description": "Pink and cyan on deep purple."
}
```

Without `theme.json`, the folder name is shown.

## Light and dark mode are required

Visitors choose *Auto*, *Light* or *Dark* in the page header. *Auto* follows their system. The
page marks the result on the `<html>` element as `data-mode="dark"` or `data-mode="light"`.
**Every theme must style both modes**, with one block each:

```css
:root[data-mode="dark"] {
  /* colours for dark mode */
}

:root[data-mode="light"] {
  /* colours for light mode */
}
```

A theme whose `theme.css` lacks either block isn't offered. The admin page lists it under the
theme picker with the reason. Only real rules count: a block that is commented out doesn't, and
neither does a `theme.css` the app can't read (check the file permissions). A block may be empty (the `default` theme's are) if the standard
colours suit that mode.

## Step by step

1. Copy the retro theme as a starting point:

   ```bash
   mkdir -p instance/themes
   cp -r lanparty/themes/retro instance/themes/neon
   ```

2. Edit `instance/themes/neon/theme.json` (name and description).
3. Change the colours in both blocks of `theme.css` (see [Tokens](#tokens)).
4. Look at it without changing any event: open `/?theme=neon` on the public page. Switch between
   *Light* and *Dark* in the header, and try a phone-sized window.
5. Pick it for an event under *Admin → Settings → Appearance → Theme of the public page*.

## Tokens

The base stylesheet (`lanparty/static/style.css`) builds everything from CSS custom properties. A
theme overrides the ones it wants. Tokens it leaves out keep the standard value for that mode.

### Colours (set them per mode)

| Token | Used for |
|---|---|
| `--bg` | Page background |
| `--card` | Cards and dialogs |
| `--border` | Borders and dividers |
| `--text` | Normal text, seat labels |
| `--muted` | Secondary text, labels |
| `--accent` / `--on-accent` | Links, primary buttons, focus rings / text on them |
| `--accent-hover` | Primary buttons under the mouse (text: `--on-accent`) |
| `--free` / `--free-hover` | Free seats (text: `--text`) |
| `--reserved` / `--on-reserved` | Reserved seats and badges / their text |
| `--paid` / `--on-paid` | Paid seats and badges / their text |
| `--mine` / `--on-mine` / `--mine-ring` | The visitor's own seat / its text / its inner ring |
| `--danger` / `--danger-text` | Outline of dangerous buttons / error text |
| `--success-text`, `--warning-text` | Success and warning messages |
| `--banner-border` | Border of warning banners |
| `--hero-from` / `--hero-to` / `--hero-text` | Header gradient / header text |
| `--input-bg`, `--button-bg` | Form fields, normal buttons |
| `--map-bg`, `--area-bg`, `--grid-line` | Seat map frame, event area, editor grid |
| `--table-bg`, `--table-border` | Tables on the seat map |
| `--backdrop` | Shade behind dialogs |

Keep text readable: foreground tokens (`--text`, `--muted`, `--on-…`, `…-text`, `--accent` as
link colour, `--hero-text`) need a **contrast ratio of at least 4.5:1** against the background
they sit on. The built-in themes are checked by `tests/test_themes.py`. Use any WCAG contrast
checker for your own.

### Shape and type (usually the same in both modes)

Put these in a plain `:root { … }` block:

| Token | Default | Effect |
|---|---|---|
| `--font-body` | `system-ui, …` | Font of the whole page |
| `--font-mono` | `ui-monospace, …` | Codes, addresses, links |
| `--radius-scale` | `1` | Multiplies every corner radius; `0` gives square corners |

## Extra CSS

Below the token blocks, `theme.css` may contain any CSS: other fonts, a background image,
animations. The retro theme, for example, adds a `> ` prompt in front of the event name, a blinking
cursor and scanlines (`body::after`). Some tips:

- Respect `@media (prefers-reduced-motion: reduce)` for animations.
- Use `--…` tokens you define yourself, per mode, for colours of your extra rules (the retro
  theme's `--glow` and `--scanline`), so both modes stay consistent.
- Check the page at 320 px width: nothing should scroll sideways.

## Fonts and images

The page's Content Security Policy only allows files from the app's own address, so **fonts and
images must be inside the theme folder**. Links to Google Fonts or other CDNs don't load. Reference
them with paths relative to `theme.css`:

```css
@font-face {
  font-family: "Neon Sans";
  src: url("neon-sans.woff2") format("woff2");
}
:root { --font-body: "Neon Sans", system-ui, sans-serif; }
```

The app serves these file types from a theme folder: `.css`, `.woff2`, `.woff`, `.ttf`, `.otf`,
`.png`, `.jpg`/`.jpeg`, `.gif`, `.webp`, `.avif` and `.svg`. Anything else (including `theme.json`)
returns 404. Check the licence of fonts and images you bundle.

## How it works

- The public page loads `/static/style.css`, then `/theme.css`. That small stylesheet imports
  `/themes/<id>/theme.css` of the active event's theme (so relative URLs in the theme resolve
  inside its folder) and sets `--lp-theme` to the theme's id, so the page knows which theme it got.
  Without an active event, or if the chosen theme has disappeared or become invalid, it falls back
  to `default`. Don't use `--lp-theme` in your own theme.
- An event keeps its theme setting even if that theme disappears later; other settings can still
  be saved, and the public page shows `default` until the theme is back or another one is picked.
- Pages that are already open follow changes: when polling shows a different theme (another event
  opened, or an admin picked another theme), the page switches its stylesheet.
- `/?theme=<id>` makes `theme.js` load that theme instead (preview); such a page keeps it.
- Theme files are sent with `Cache-Control: no-cache`, so edits show up on the next page load.
- A new event copies the theme of the event it is based on.
