#!/usr/bin/env node
// Compatibility entry point. The app uses frontend/src/design-tokens.css;
// generated-theme.css and generated-tailwind-theme.mjs are unused legacy artifacts.
// Keep those Classic artifacts untouched; use the canonical deterministic generator.
import './generate.mjs'
