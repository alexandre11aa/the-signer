"""Central place for limits and constants."""
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent   # .../app
STATIC_DIR = APP_DIR / "static"
TEMPLATE_DIR = APP_DIR / "template"
ADOBE_LOGO_PATH = STATIC_DIR / "img" / "adobe_logo_transparente.png"

# Request limits (the JSON body carries base64 files, so it is ~1.37x bigger).
MAX_BODY_BYTES = 40 * 1024 * 1024   # whole request body
MAX_PDF_BYTES = 25 * 1024 * 1024    # decoded PDF
MAX_PFX_BYTES = 256 * 1024          # decoded certificate
MAX_PAGES_TO_SIGN = 100
SIGN_TIMEOUT_SECONDS = 60

# Signature box geometry (PDF points).
STAMP_WIDTH, STAMP_HEIGHT, STAMP_MARGIN = 210, 60, 20

# Position number -> (row, column). Row: 0 top..2 bottom | column: 0 left..2 right.
POSITIONS = {
    1: (0, 0), 2: (0, 1), 3: (0, 2),
    4: (1, 0), 5: (1, 1), 6: (1, 2),
    7: (2, 0), 8: (2, 1), 9: (2, 2),
}

STYLES = ("default", "adobe", "custom")

# "custom" style: user-supplied PNG, drawn where the Adobe symbol goes.
CUSTOM_LOGO_PIXELS = (264, 264)
MAX_LOGO_BYTES = 512 * 1024
