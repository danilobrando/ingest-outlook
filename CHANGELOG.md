# Changelog

## 0.5.0

- Fixed UTC calendar timestamps without a `Z` suffix being rendered as local time.
- Added `calendar --ahead 0..60`; calendar windows now use local-day boundaries, so the lookback starts at 00:00 local and the range can include future days.
- Added a persistent `configure` command with explicit value-source reporting.
- Added the corporate read-only profile and local blocking for all Graph write commands.
- Added configurable vault/output paths and backward-compatible ingestion payloads.
- Added Windows-safe console, paths, filenames, token checks, locking behavior, and no-admin PowerShell installation.
- Fixed false scope drift by checking the effective configured scopes.
- Fixed Windows permission diagnostics that previously attempted ineffective `chmod` repairs.
- Replaced hardcoded recovery paths with the active interpreter and resolved script path.
- Added fake Graph/token integration tests and Linux, macOS, and Windows CI coverage.
- Added corporate Entra setup and Spanish Windows installation documentation.

## 0.4.0

- Initial public release.
