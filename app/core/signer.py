"""PDF signing logic. Works exclusively on in-memory bytes: nothing touches the disk."""
import io
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from secrets import token_hex

from PIL import Image
from pyhanko.pdf_utils import layout
from pyhanko.pdf_utils.font.basic import SimpleFontEngineFactory
from pyhanko.pdf_utils.images import PdfImage
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.pdf_utils.layout import BoxConstraints
from pyhanko.pdf_utils.text import TextBox, TextBoxStyle
from pyhanko.sign import fields, signers
from pyhanko.stamp import TextStamp, TextStampStyle
from pyhanko.pdf_utils.reader import PdfFileReader
from pypdf import PdfReader, PdfWriter

from .config import ADOBE_LOGO_PATH, CUSTOM_LOGO_PIXELS, STAMP_HEIGHT, STAMP_WIDTH
from .utils import UserInputError, parse_pages, signature_box

# Adobe-like layout (points): name on the left, details on the right, logo in between.
NAME_COLUMN_WIDTH, DETAILS_X = 82, 90
LOGO_SIZE, LOGO_BOTTOM = 46, 3
LOGO_X = (NAME_COLUMN_WIDTH + DETAILS_X) // 2 - LOGO_SIZE // 2
ADOBE_WIDTH, ADOBE_HEIGHT = DETAILS_X + 118, 52
HELVETICA = SimpleFontEngineFactory("Helvetica", 0.52)


class AdobeStamp(TextStamp):
    """Large name on the left, small details on the right."""

    def _render_inner_content(self):
        w, h = self.box.width, self.box.height
        rule = layout.SimpleBoxLayoutRule(
            x_align=layout.AxisAlignment.ALIGN_MIN,
            y_align=layout.AxisAlignment.ALIGN_MID,
            margins=layout.Margins(0, 0, 0, 0),
            inner_content_scaling=layout.InnerScaling.NO_SCALING,
        )

        def text_box(text, font_size, leading, width):
            box = TextBox(
                TextBoxStyle(font_size=font_size, leading=leading, font=HELVETICA,
                             box_layout_rule=rule),
                writer=self.writer, resources=self.resources,
                box=BoxConstraints(width=int(width), height=int(h)),
            )
            box.content = text
            return box.render()

        name_cmd = text_box(self.text_params["wrapped_name"], 10, 11.2, NAME_COLUMN_WIDTH)
        details_cmd = text_box(self.text_params["details"], 7, 8.2, w - DETAILS_X)
        return [
            b"q 1 0 0 1 0 0 cm", name_cmd, b"Q",
            b"q 1 0 0 1 %d 0 cm" % DETAILS_X, details_cmd, b"Q",
        ]


@dataclass(frozen=True)
class AdobeStampStyle(TextStampStyle):
    def create_stamp(self, writer, box, text_params):
        return AdobeStamp(writer=writer, style=self, box=box, text_params=text_params)


def wrap_text(text: str, max_chars: int = 16) -> str:
    lines, current = [], ""
    for word in text.split():
        if current and len(current) + 1 + len(word) > max_chars:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return "\n".join(lines)


def adobe_date(tz_offset_minutes: int = 0) -> str:
    """Current time in the client's timezone, formatted like Adobe (2024.01.31 08:31:48 -03'00')."""
    now = datetime.now(timezone(timedelta(minutes=tz_offset_minutes)))
    offset = now.strftime("%z")
    return now.strftime("%Y.%m.%d %H:%M:%S ") + f"{offset[:3]}'{offset[3:]}'"


def load_signer(pfx_bytes: bytes, password: str) -> signers.SimpleSigner:
    try:
        return signers.SimpleSigner.load_pkcs12_data(
            pfx_bytes, other_certs=None, passphrase=password.encode("utf-8"))
    except Exception:
        # Never echo the underlying error: it may carry details of the key material.
        raise UserInputError("invalid_certificate_or_password")


def count_pages(pdf_bytes: bytes) -> tuple[int, bool]:
    """Returns (page_count, is_encrypted)."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        if reader.is_encrypted:
            return 0, True
        return len(reader.pages), False
    except Exception:
        raise UserInputError("invalid_pdf")


def load_custom_logo(data: bytes) -> Image.Image:
    """Accepts only a PNG of exactly CUSTOM_LOGO_PIXELS. The size is read from the header
    before any pixel data is decoded."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG" or image.size != CUSTOM_LOGO_PIXELS:
                raise UserInputError("invalid_logo")
            return image.convert("RGBA")
    except UserInputError:
        raise
    except Exception:
        raise UserInputError("invalid_logo")


def _build_style(style: str, signer_name: str, custom_logo: Image.Image | None = None,
                tz_offset_minutes: int = 0):
    if style in ("adobe", "custom"):
        # "custom" uses the Adobe layout; only the symbol image changes.
        symbol = custom_logo if custom_logo is not None else Image.open(ADOBE_LOGO_PATH).convert("RGBA")
        logo = PdfImage(symbol)
        stamp_style = AdobeStampStyle(
            stamp_text="",
            border_width=0,
            background=logo,
            background_opacity=1.0,   # logo drawn as-is: its softness is in the PNG, no extra opacity layer
            background_layout=layout.SimpleBoxLayoutRule(
                x_align=layout.AxisAlignment.ALIGN_MID,
                y_align=layout.AxisAlignment.ALIGN_MID,
                margins=layout.Margins(
                    left=LOGO_X, right=ADOBE_WIDTH - LOGO_X - LOGO_SIZE,
                    bottom=round(LOGO_BOTTOM),
                    top=round(ADOBE_HEIGHT - LOGO_BOTTOM - LOGO_SIZE)),
                inner_content_scaling=layout.InnerScaling.SHRINK_TO_FIT),
        )
        params = {
            "wrapped_name": wrap_text(signer_name),
            "details": ("Assinado de forma digital\n"
                        + wrap_text(f"por {signer_name}", 22)
                        + f"\nDados: {adobe_date(tz_offset_minutes)}"),
        }
        return stamp_style, params, (ADOBE_WIDTH, ADOBE_HEIGHT)

    stamp_style = TextStampStyle(
        stamp_text="Assinado de forma digital por\n%(signer)s\nDados: %(ts)s",
        text_box_style=TextBoxStyle(font_size=8),
        border_width=0,
        background_opacity=0,
    )
    # Default style: pyHanko would render the time in the server's timezone, so pass our own.
    params = {"signer": signer_name, "ts": adobe_date(tz_offset_minutes)}
    return stamp_style, params, (STAMP_WIDTH, STAMP_HEIGHT)


def normalize_hybrid_xref(pdf_bytes: bytes) -> bytes:
    """pyHanko refuses to update PDFs with a hybrid cross-reference section (classic xref
    table + /XRefStm), because such files cannot be extended safely. For unsigned PDFs we
    rewrite the file once with pypdf, which drops the hybrid structure and keeps the content.
    A PDF that is both hybrid and already signed is rejected: rewriting it would invalidate
    the existing signatures."""
    try:
        reader = PdfFileReader(io.BytesIO(pdf_bytes))
        if not reader.xrefs.hybrid_xrefs_present:
            return pdf_bytes
        if reader.embedded_signatures:
            raise UserInputError("unsupported_pdf")
        writer = PdfWriter(clone_from=io.BytesIO(pdf_bytes))
        output = io.BytesIO()
        writer.write(output)
        return output.getvalue()
    except UserInputError:
        raise
    except Exception:
        raise UserInputError("unsupported_pdf")


def sign_pdf(pdf_bytes: bytes, pfx_bytes: bytes, password: str,
             pages: str, position: int, style: str, logo: bytes | None = None,
             tz_offset_minutes: int = 0) -> bytes:
    """One visible signature per selected page, as chained incremental updates."""
    total, encrypted = count_pages(pdf_bytes)
    if encrypted:
        raise UserInputError("encrypted_pdf")
    page_indexes = parse_pages(pages, total)

    signer = load_signer(pfx_bytes, password)
    signer_name = signer.signing_cert.subject.native.get("common_name", "Signer")
    custom_logo = None
    if style == "custom":
        if not logo:
            raise UserInputError("invalid_logo")
        custom_logo = load_custom_logo(logo)
    stamp_style, params, (width, height) = _build_style(
        style, signer_name, custom_logo, tz_offset_minutes)

    pdf_bytes = normalize_hybrid_xref(pdf_bytes)
    reader = PdfReader(io.BytesIO(pdf_bytes))
    current = io.BytesIO(pdf_bytes)
    try:
        for index in page_indexes:
            field_name = f"Signature_p{index + 1}_{token_hex(4)}"
            box = signature_box(reader.pages[index], position, width, height)
            writer = IncrementalPdfFileWriter(current)
            metadata = signers.PdfSignatureMetadata(field_name=field_name)
            pdf_signer = signers.PdfSigner(
                metadata, signer=signer, stamp_style=stamp_style,
                new_field_spec=fields.SigFieldSpec(field_name, on_page=index, box=box))
            output = io.BytesIO()
            pdf_signer.sign_pdf(writer, output=output, appearance_text_params=params)
            current = output
    except UserInputError:
        raise
    except Exception:
        # Library errors are mapped to one fixed code; their text is never exposed.
        raise UserInputError("signing_failed")
    return current.getvalue()
