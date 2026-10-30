# ODIN Classic (October 2026)

A snapshot of ODIN's web theme as of 2.0.35, kept so it can be restored or offered as a selectable theme if a future overhaul changes the default look. Requested by Shane on 2026-10-29.

- `tokens.json`: the design tokens (design/tokens.json at the time). Warm "farm" neutrals, amber primary #C47A1A, IBM Plex Sans/Mono in the tokens, and status colours.
- `design-tokens.css`: the generated CSS variables for dark, light and high-contrast.
- Screenshots: printer card in dark, high-contrast and phone width, from the A03 accessibility run.
- Text contrast: secondary #bcb7aa and muted #a8a294 pass WCAG AA (≥ 4.5:1) on card, content and input backgrounds.

Known drift at snapshot time: the iOS/macOS apps use the same amber accent, but cool system neutrals and SF fonts. The web's stored branding renders system-ui rather than the IBM Plex fonts in the tokens. A future shared design system should come from one token source for web, iOS and macOS.
