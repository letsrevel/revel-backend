"""Shared banned-hex list for the email/PDF branding guard tests (see docs/brand-style-guide.md)."""

# Pre-rebrand accents that must never reappear in rendered output. Union of the
# per-file tuples the branding guards used to each declare on their own.
LEGACY_BRAND_HEXES = (
    "#2196F3",
    "#28a745",
    "#3498db",
    "#4CAF50",
    "#667eea",
    "#764ba2",
    "#dc3545",
    "#ff9800",
)
