# Revel Brand Style Guide — Technical Tokens

Source of truth: **Revel Digital Brand Styleguide** (Isabella Radich, 2026; InDesign
source + PDF kept locally in `local_stuff/Revel_Digital-Styleguide/`, not committed).
An earlier deck (*Branding + Social Media Design*, Acid Hairs, 2026-06-24) shows the
same palette. This file records the technical values so code never has to rediscover
them.

## Colors

| Role      | Name          | Hex       | RGB             |
| --------- | ------------- | --------- | --------------- |
| Primary   | Hearty Purple | `#8C3CDD` | `140, 60, 221`  |
| Primary   | Light Crimson | `#E6332A` | `230, 51, 42`   |
| Secondary | Lavender      | `#AB82DB` | `171, 130, 219` |
| Secondary | Periwinkle    | `#9AB2FF` | `154, 178, 255` |
| Highlight | Amber         | `#F9B233` | `249, 178, 51`  |
| Text      | Ink           | `#0D1E1C` | `13, 30, 28`    |
| Text      | White         | `#FFFFFF` | `255, 255, 255` |

**The hex values are the contract.** The style-guide PDF's printed RGB lines contain
typos (it prints "230, 52, 42" for Light Crimson and "1171, 130, 219" for Lavender);
the hexes and the InDesign swatches agree on the values in the table above. Don't
re-derive these colors from HSL tokens either — HSL→RGB truncation lands one unit off
(e.g. `#8C3BDC` instead of `#8C3CDD`).

## Brand gradient

**Linear, vertical: Hearty Purple (top) → Light Crimson (bottom), midpoint at 50%**
("Logo Gradient" page; the InDesign gradient swatch "Verlauf" runs Violett→Rot with
these exact stops). This is the gradient of the RevelMark logo and the guide's cover.
The older branding deck showed a diagonal-looking swatch — vertical is correct.

## Logo

- RevelMark "R" with heart counter, filled with the brand gradient; wordmark
  "let's revel." underneath (Nata Sans: "let's" Light, "revel" Semibold, tracking 60).
- Safe zone: ½× on all sides, where × is the height of the heart counter.
- Don't crowd the logo against edges or other elements (see the guide's do/don't pages).

## Typography

- **Nata Sans** — main brand font, used universally (web + social). All family styles
  allowed. Tracking 0 everywhere except the logo wordmark (60).
- **BBH Bartle** — additional display font for social media designs, upper and
  lowercase, tracking 0.
- Both are open-source Google Fonts.

## Picture style

- **Do:** colourful, warm image language; closeness and a sense of community;
  "real"-looking portraits.
- **Don't:** classic stock imagery; people who feel like actors or models; cold,
  distanced shots; corporate casual.

## Where these tokens live in code

- Backend wallet passes: `src/wallet/apple/formatting.py`
  (`_HEARTY_PURPLE_RGB`, `_LIGHT_CRIMSON_RGB`; gradient endpoints via
  `get_gradient_rgb()`, Google-rail hex via `get_theme_hex_background()`); vertical
  gradient rendering in `src/wallet/apple/images.py` (`generate_gradient_background`).
- Frontend: `revel-frontend/src/app.css` (`--logo-from`/`--logo-to`,
  `--poster-purple`, `--poster-crimson`, `--poster-lavender`, `--poster-periwinkle`,
  `--poster-amber`, `--poster-ink`). The frontend derives colors from HSL tokens, so
  some render one RGB unit off the table above; there the HSL tokens are the contract,
  and AA-contrast variants exist deliberately (e.g. `--poster-crimson-deep`
  `hsl(3 79% 50%)` for white text panels).
- Accessibility floor: white text on Hearty Purple ≈ 5.5:1 (AA for normal text);
  white on raw Light Crimson ≈ 4.3:1 (fails AA normal text — use a deepened variant
  behind white copy, as the frontend does).
