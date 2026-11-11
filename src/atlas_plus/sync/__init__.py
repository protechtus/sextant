"""Sextant Sync: remote reach, device check-in, and system-to-system sharing.

Design: docs/SEXTANT-SYNC-DESIGN-2026-08.md. The corpus (markdown files under
the memory root) is the only thing that syncs; derived state (.atlas/) is
rebuilt per device. Every boundary crossing goes through policy.deny_reason,
every applied write goes through apply.apply_change (version-before-write,
archive-not-delete, quarantine on conflict).
"""

PROTOCOL_VERSION = 1
