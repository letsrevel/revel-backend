"""Placeholder file generation for file uploads."""

from events.management.commands.seeder.base import BaseSeeder

# Minimal valid 1x1 transparent PNG (67 bytes)
# Created with: convert -size 1x1 xc:transparent minimal.png
MINIMAL_PNG = bytes(
    [
        0x89,
        0x50,
        0x4E,
        0x47,
        0x0D,
        0x0A,
        0x1A,
        0x0A,  # PNG signature
        0x00,
        0x00,
        0x00,
        0x0D,
        0x49,
        0x48,
        0x44,
        0x52,  # IHDR chunk
        0x00,
        0x00,
        0x00,
        0x01,
        0x00,
        0x00,
        0x00,
        0x01,  # 1x1
        0x08,
        0x06,
        0x00,
        0x00,
        0x00,
        0x1F,
        0x15,
        0xC4,
        0x89,  # 8-bit RGBA
        0x00,
        0x00,
        0x00,
        0x0A,
        0x49,
        0x44,
        0x41,
        0x54,  # IDAT chunk
        0x78,
        0x9C,
        0x63,
        0x00,
        0x01,
        0x00,
        0x00,
        0x05,
        0x00,
        0x01,  # compressed data
        0x0D,
        0x0A,
        0x2D,
        0xB4,  # CRC
        0x00,
        0x00,
        0x00,
        0x00,
        0x49,
        0x45,
        0x4E,
        0x44,  # IEND chunk
        0xAE,
        0x42,
        0x60,
        0x82,  # CRC
    ]
)

# Minimal valid PDF (around 200 bytes)
MINIMAL_PDF = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R>>endobj
xref
0 4
0000000000 65535 f
0000000009 00000 n
0000000052 00000 n
0000000101 00000 n
trailer<</Size 4/Root 1 0 R>>
startxref
167
%%EOF"""


class FileSeeder(BaseSeeder):
    """Seeder for generating placeholder files."""

    def seed(self) -> None:
        """Pre-generate placeholder files in state."""
        self.log("Generating placeholder files...")
        self.state.placeholder_png = MINIMAL_PNG
        self.state.placeholder_pdf = MINIMAL_PDF
        self.log(f"  PNG: {len(MINIMAL_PNG)} bytes")
        self.log(f"  PDF: {len(MINIMAL_PDF)} bytes")
