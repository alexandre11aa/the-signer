"""Pure helpers: page selection parsing and signature box geometry."""
from .config import MAX_PAGES_TO_SIGN, POSITIONS, STAMP_MARGIN


class UserInputError(Exception):
    """Raised for invalid user input. `code` is a stable, non-sensitive identifier."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def parse_pages(text: str, total_pages: int) -> list[int]:
    """'1,3,5-8' or 'all' -> sorted zero-based page indexes."""
    text = text.strip().lower()
    if text in ("all", "*"):
        indexes = list(range(total_pages))
    else:
        selected: set[int] = set()
        for part in text.replace(" ", "").split(","):
            if not part:
                continue
            try:
                if "-" in part:
                    start_s, end_s = part.split("-", 1)
                    start, end = int(start_s), int(end_s)
                else:
                    start = end = int(part)
            except ValueError:
                raise UserInputError("invalid_pages")
            if not (1 <= start <= end <= total_pages):
                raise UserInputError("pages_out_of_range")
            selected.update(range(start - 1, end))
        indexes = sorted(selected)
    if not indexes:
        raise UserInputError("invalid_pages")
    if len(indexes) > MAX_PAGES_TO_SIGN:
        raise UserInputError("too_many_pages")
    return indexes


def signature_box(page, position: int, width: float, height: float) -> tuple[int, int, int, int]:
    """Rectangle (x1, y1, x2, y2) in page space, honoring the page /Rotate value."""
    row, col = POSITIONS[position]
    media = page.mediabox
    x0, y0 = float(media.left), float(media.bottom)
    w, h = float(media.width), float(media.height)
    rotation = (page.get("/Rotate", 0) or 0) % 360

    # Work in the visual space (as the user sees the page), then map back.
    vw, vh = (h, w) if rotation in (90, 270) else (w, h)
    m = STAMP_MARGIN
    vx = m if col == 0 else (vw - width) / 2 if col == 1 else vw - width - m
    vy = vh - height - m if row == 0 else (vh - height) / 2 if row == 1 else m

    points = []
    for cx, cy in ((vx, vy), (vx + width, vy + height)):
        if rotation == 0:
            px, py = cx, cy
        elif rotation == 90:
            px, py = w - cy, cx
        elif rotation == 180:
            px, py = w - cx, h - cy
        else:  # 270
            px, py = cy, h - cx
        points.append((x0 + px, y0 + py))
    (xa, ya), (xb, yb) = points
    return tuple(round(v) for v in (min(xa, xb), min(ya, yb), max(xa, xb), max(ya, yb)))
