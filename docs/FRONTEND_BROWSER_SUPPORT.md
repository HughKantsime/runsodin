# Frontend browser support

Starting with ODIN v1.9.18, the frontend uses Tailwind CSS 4.3.3 and requires:

- Safari 16.4 or later.
- Chrome/Chromium 111 or later (including Chromium-based managed browsers).
- Firefox 128 or later.

These are the upstream CSS feature requirements; they are not evidence of
ODIN tests run on every minimum browser version. Current release verification
uses Chromium. Older browsers may render incorrectly and are not supported.
Schools should verify their managed-browser versions before upgrading.

The migration removes the vulnerable Tailwind 3 development dependency chain;
it does not extend the v1.9.17 build-only exception. The existing token-backed
palette, type sizes, radii, shadows and keyboard focus behavior are retained.
`frontend/tailwind-legacy-theme.json` holds only the prior theme values needed
to preserve the UI; it contains no Tailwind 3 runtime or dependency code.

Source: [Tailwind upgrade guide](https://tailwindcss.com/docs/upgrade-guide).
