# Changelog

All notable changes to PrintFarm Scheduler will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-01-29

### Added
- Spool management system (replaces Spoolman dependency)
  - Individual spool tracking with QR codes
  - Weight tracking and usage history
  - Load/unload spools to printer slots
  - QR label generation for printing
- Audit logging for compliance (CMMC Level 2 ready)
- AMS sync mismatch detection
- Encrypted printer credentials at rest

### Changed
- Improved color analysis for olive/muted greens
- Support materials now show "Natural" instead of black

### Fixed
- PLA-S correctly maps to PLA_SUPPORT filament type

## [0.2.0] - 2026-01-28

### Added
- Bambu Lab AMS integration via local MQTT
- Expanded FilamentType enum for Bambu codes (PLA-S, PA-CF, etc.)
- Filament library with local matching priority
- Color analysis fallback for unknown filaments

## [0.1.0] - 2026-01-27

### Added
- Initial release
- Printer management
- Job scheduling
- Timeline view
- Spoolman integration (optional)
