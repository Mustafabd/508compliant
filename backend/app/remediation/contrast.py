"""WCAG 2.1 color-contrast checking for text found in a PDF's content
streams.

For each text-showing operator (`Tj`/`TJ`/`'`/`"`), this walks the page's
graphics state (CTM, fill/stroke color and colorspace, text matrix) in
painting order to determine: the color the text was painted with, and the
color of whatever was painted underneath it (an explicit filled
rectangle/path if one covers that point, otherwise the bare page --
white -- if nothing was painted there at all). It computes the WCAG
contrast ratio between the two and classifies each run as pass, fail, or
"needs manual review" when the background (or occasionally the text
color itself, e.g. a Separation/DeviceN spot color or a pattern fill)
can't be reliably determined -- images, shadings, and patterns are never
guessed at.

Scope/known limitations (consistent with the rest of the remediation
pipeline's heuristic approach):
  - Text inside Form XObjects is not recursed into (same limitation as
    the tagger).
  - A text run's position is approximated by its baseline origin point,
    not a full glyph-by-glyph bounding box.
  - Font size is the raw `Tf` operand, not scaled by the CTM (matches
    the heading-detection heuristic in content.py).
  - Text render mode (Tr) is tracked as simple running state, not part
    of the q/Q graphics-state stack (a mid-run Tr change inside a saved
    state is rare and low-impact if missed).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import pikepdf
from pikepdf import Dictionary, Name

TEXT_SHOW_OPS = {"Tj", "TJ", "'", '"'}

AA_NORMAL_THRESHOLD = 4.5
AA_LARGE_THRESHOLD = 3.0
LARGE_TEXT_MIN_SIZE = 18.0
LARGE_TEXT_BOLD_MIN_SIZE = 14.0

WHITE = (1.0, 1.0, 1.0)
BLACK = (0.0, 0.0, 0.0)

Matrix = tuple  # (a, b, c, d, e, f)
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
RGB = tuple  # (r, g, b) each 0..1


@dataclass
class ContrastFinding:
    page: int
    status: str  # "fail" | "needs_review"
    ratio: float | None
    threshold: float | None
    text_preview: str
    font_size: float
    is_bold: bool
    reason: str = ""


@dataclass
class ContrastStats:
    checked: int = 0
    passed: int = 0
    failed: int = 0
    needs_review: int = 0
    findings: list = field(default_factory=list)


# --- 2D affine matrix helpers (PDF row-vector convention: [x y 1] x M) ---

def _num(x) -> float:
    if isinstance(x, Decimal):
        return float(x)
    return float(x)


def mat_mult(m1: Matrix, m2: Matrix) -> Matrix:
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def apply_point(m: Matrix, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def translate(tx: float, ty: float) -> Matrix:
    return (1.0, 0.0, 0.0, 1.0, tx, ty)


# --- Color math ---

def _srgb_to_linear(c: float) -> float:
    c = min(max(c, 0.0), 1.0)
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: RGB) -> float:
    r, g, b = (_srgb_to_linear(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(rgb1: RGB, rgb2: RGB) -> float:
    l1, l2 = relative_luminance(rgb1), relative_luminance(rgb2)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def cmyk_to_rgb(c: float, m: float, y: float, k: float) -> RGB:
    return ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))


def lab_to_rgb(l: float, a: float, b: float) -> RGB:
    # D50 white point (PDF default); standard CIE Lab -> sRGB conversion.
    fy = (l + 16) / 116
    fx = fy + a / 500
    fz = fy - b / 200

    def finv(t):
        return t ** 3 if t ** 3 > 0.008856 else (t - 16 / 116) / 7.787

    xn, yn, zn = 0.9642, 1.0, 0.8249
    x, y_, z = finv(fx) * xn, finv(fy) * yn, finv(fz) * zn

    r = x * 3.1338561 - y_ * 1.6168667 - z * 0.4906146
    g = -x * 0.9787684 + y_ * 1.9161415 + z * 0.0334540
    bl = x * 0.0719453 - y_ * 0.2289914 + z * 1.4052427

    def gamma(c):
        c = min(max(c, 0.0), 1.0)
        return 1.055 * c ** (1 / 2.4) - 0.055 if c > 0.0031308 else 12.92 * c

    return (gamma(r), gamma(g), gamma(bl))


# --- Colorspace resolution ---

DEVICE_GRAY_NAMES = {"/DeviceGray", "/CalGray", "/G"}
DEVICE_RGB_NAMES = {"/DeviceRGB", "/CalRGB", "/RGB"}
DEVICE_CMYK_NAMES = {"/DeviceCMYK", "/CMYK"}


def resolve_colorspace(name: str, resources) -> object:
    """Resolve a `cs`/`CS` operand (a resource name) to either a known
    device-space string or the underlying colorspace Array/Name object."""
    if name in DEVICE_GRAY_NAMES or name in DEVICE_RGB_NAMES or name in DEVICE_CMYK_NAMES or name == "/Pattern":
        return name
    try:
        cs_dict = resources.get("/ColorSpace")
        if cs_dict is not None:
            return cs_dict.get(Name(name), name)
    except Exception:
        pass
    return name


def _indexed_lookup_bytes(lookup) -> bytes | None:
    try:
        if isinstance(lookup, pikepdf.Stream):
            return bytes(lookup.read_bytes())
        return bytes(lookup)
    except Exception:
        return None


def color_from_components(cs, components: list) -> RGB | None:
    """Best-effort resolution of a colorspace + numeric operands to RGB.
    Returns None when the colorspace can't be reliably resolved (e.g.
    Separation/DeviceN spot colors needing a tint-transform function, or
    Pattern fills) -- callers must treat None as "unknown", not "black"."""
    if cs is None:
        return None

    if isinstance(cs, pikepdf.Name):
        cs = str(cs)

    if isinstance(cs, str):
        if cs in DEVICE_GRAY_NAMES:
            return (components[0],) * 3 if components else None
        if cs in DEVICE_RGB_NAMES:
            return tuple(components[:3]) if len(components) >= 3 else None
        if cs in DEVICE_CMYK_NAMES:
            return cmyk_to_rgb(*components[:4]) if len(components) >= 4 else None
        return None  # "/Pattern" or unrecognized name

    try:
        family = str(cs[0])
    except Exception:
        return None

    if family == "/ICCBased":
        try:
            n = int(cs[1].get("/N", 3))
        except Exception:
            n = 3
        if n == 1:
            return (components[0],) * 3 if components else None
        if n == 4:
            return cmyk_to_rgb(*components[:4]) if len(components) >= 4 else None
        return tuple(components[:3]) if len(components) >= 3 else None

    if family in ("/CalGray",):
        return (components[0],) * 3 if components else None
    if family in ("/CalRGB",):
        return tuple(components[:3]) if len(components) >= 3 else None
    if family == "/Lab":
        return lab_to_rgb(*components[:3]) if len(components) >= 3 else None

    if family == "/Indexed":
        try:
            base = cs[1]
            lookup = cs[3]
            index = int(components[0])
            base_n = _colorspace_components(base)
            raw = _indexed_lookup_bytes(lookup)
            if raw is None or base_n is None:
                return None
            offset = index * base_n
            if offset + base_n > len(raw):
                return None
            comps = [b / 255.0 for b in raw[offset:offset + base_n]]
            return color_from_components(base, comps)
        except Exception:
            return None

    # Separation, DeviceN (needs a tint-transform function we don't
    # evaluate), Pattern, or anything else unrecognized.
    return None


def _colorspace_components(cs) -> int | None:
    if isinstance(cs, pikepdf.Name):
        cs = str(cs)
    if isinstance(cs, str):
        if cs in DEVICE_GRAY_NAMES:
            return 1
        if cs in DEVICE_RGB_NAMES:
            return 3
        if cs in DEVICE_CMYK_NAMES:
            return 4
        return None
    try:
        family = str(cs[0])
    except Exception:
        return None
    if family == "/ICCBased":
        try:
            return int(cs[1].get("/N", 3))
        except Exception:
            return 3
    if family in ("/CalGray",):
        return 1
    if family in ("/CalRGB", "/Lab"):
        return 3
    return None


# --- Font helpers ---

def _font_is_bold(font_dict) -> bool:
    if font_dict is None:
        return False
    try:
        base_font = str(font_dict.get("/BaseFont", ""))
        if any(tag in base_font for tag in ("Bold", "bold", "Black", "Heavy")):
            return True
    except Exception:
        pass
    descriptor = None
    try:
        descriptor = font_dict.get("/FontDescriptor")
        if descriptor is None and "/DescendantFonts" in font_dict:
            descendant = font_dict["/DescendantFonts"][0]
            descriptor = descendant.get("/FontDescriptor")
    except Exception:
        descriptor = None
    if descriptor is not None:
        try:
            flags = int(descriptor.get("/Flags", 0))
            if flags & 0x40000:  # ForceBold
                return True
        except Exception:
            pass
        try:
            weight = descriptor.get("/StemV")
            if weight is not None and int(weight) >= 140:
                return True
        except Exception:
            pass
    return False


def _decode_text_preview(operands, font_is_simple: bool) -> str:
    parts = []
    for op in operands:
        if isinstance(op, pikepdf.Array):
            for item in op:
                if isinstance(item, (pikepdf.String, str, bytes)):
                    parts.append(item)
        elif isinstance(op, (pikepdf.String, str, bytes)):
            parts.append(op)
    raw = "".join(str(p) if not isinstance(p, bytes) else p.decode("latin-1", "replace") for p in parts)
    if not font_is_simple:
        # Composite (Type0/CID) fonts use multi-byte codes; str(pikepdf.String)
        # already best-effort decodes, but for CID fonts that's unreliable --
        # only trust it if it came out looking like plausible text.
        if not raw.isprintable() or not raw.strip():
            return "(text preview unavailable for this font's encoding)"
    text = raw.strip()
    if len(text) > 80:
        text = text[:77] + "..."
    return text or "(empty)"


# --- Background rectangle tracking ---

@dataclass
class BackgroundRect:
    bbox: tuple  # (min_x, min_y, max_x, max_y) in page space
    color: RGB | None  # None means "something was painted here but we
                        # don't know what color" (image/shading/pattern)


def _bbox_of(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _contains(bbox, x, y) -> bool:
    min_x, min_y, max_x, max_y = bbox
    return min_x <= x <= max_x and min_y <= y <= max_y


def _background_at(rects: list, x: float, y: float) -> tuple[str, RGB | None]:
    """Search painted rects in reverse (most recent first). Returns
    (kind, color) where kind is "explicit" (found a solid fill),
    "unknown" (found an image/shading/pattern region), or "blank"
    (nothing painted there -- the bare white page)."""
    for rect in reversed(rects):
        if _contains(rect.bbox, x, y):
            return ("unknown", None) if rect.color is None else ("explicit", rect.color)
    return ("blank", WHITE)


# --- Main per-page walk ---

@dataclass
class _GState:
    ctm: Matrix
    fill_color: RGB | None
    fill_cs: object
    stroke_color: RGB | None
    stroke_cs: object


def _classify(ratio: float, font_size: float, is_bold: bool) -> tuple[bool, float]:
    is_large = font_size >= LARGE_TEXT_MIN_SIZE or (is_bold and font_size >= LARGE_TEXT_BOLD_MIN_SIZE)
    threshold = AA_LARGE_THRESHOLD if is_large else AA_NORMAL_THRESHOLD
    return ratio >= threshold, threshold


def check_page(pdf: pikepdf.Pdf, page, page_number: int, stats: ContrastStats) -> None:
    try:
        instructions = pikepdf.parse_content_stream(page)
    except Exception:
        return

    resources = page.obj.get("/Resources", Dictionary())
    fonts = resources.get("/Font", Dictionary()) if resources else Dictionary()
    mediabox = tuple(_num(v) for v in page.mediabox)
    page_bbox = (mediabox[0], mediabox[1], mediabox[2], mediabox[3])

    gstate = _GState(ctm=IDENTITY, fill_color=BLACK, fill_cs="/DeviceGray",
                      stroke_color=BLACK, stroke_cs="/DeviceGray")
    gstack: list[_GState] = []
    background_rects: list[BackgroundRect] = []
    path_points: list[tuple[float, float]] = []

    text_matrix = IDENTITY
    line_matrix = IDENTITY
    font_size = 12.0
    leading = 0.0
    render_mode = 0
    current_font_dict = None
    current_font_simple = True

    def add_path_point(x, y):
        path_points.append(apply_point(gstate.ctm, _num(x), _num(y)))

    def flush_fill(color):
        nonlocal path_points
        if len(path_points) >= 2:
            bbox = _bbox_of(path_points)
            if (bbox[2] - bbox[0]) > 1e-6 and (bbox[3] - bbox[1]) > 1e-6:
                background_rects.append(BackgroundRect(bbox=bbox, color=color))
        path_points = []

    for instr in instructions:
        op = str(instr.operator)
        operands = instr.operands

        if op == "q":
            gstack.append(_GState(**vars(gstate)))
        elif op == "Q":
            if gstack:
                gstate = gstack.pop()
        elif op == "cm":
            m = tuple(_num(v) for v in operands)
            gstate.ctm = mat_mult(m, gstate.ctm)

        elif op == "g":
            gstate.fill_color = (_num(operands[0]),) * 3
            gstate.fill_cs = "/DeviceGray"
        elif op == "G":
            gstate.stroke_color = (_num(operands[0]),) * 3
            gstate.stroke_cs = "/DeviceGray"
        elif op == "rg":
            gstate.fill_color = tuple(_num(v) for v in operands[:3])
            gstate.fill_cs = "/DeviceRGB"
        elif op == "RG":
            gstate.stroke_color = tuple(_num(v) for v in operands[:3])
            gstate.stroke_cs = "/DeviceRGB"
        elif op == "k":
            gstate.fill_color = cmyk_to_rgb(*(_num(v) for v in operands[:4]))
            gstate.fill_cs = "/DeviceCMYK"
        elif op == "K":
            gstate.stroke_color = cmyk_to_rgb(*(_num(v) for v in operands[:4]))
            gstate.stroke_cs = "/DeviceCMYK"
        elif op == "cs":
            gstate.fill_cs = resolve_colorspace(str(operands[0]), resources)
            gstate.fill_color = None
        elif op == "CS":
            gstate.stroke_cs = resolve_colorspace(str(operands[0]), resources)
            gstate.stroke_color = None
        elif op in ("sc", "scn"):
            nums = [_num(v) for v in operands if not isinstance(v, (pikepdf.Name,))]
            gstate.fill_color = color_from_components(gstate.fill_cs, nums)
        elif op in ("SC", "SCN"):
            nums = [_num(v) for v in operands if not isinstance(v, (pikepdf.Name,))]
            gstate.stroke_color = color_from_components(gstate.stroke_cs, nums)

        elif op == "re":
            x, y, w, h = (_num(v) for v in operands[:4])
            for corner in ((x, y), (x + w, y), (x + w, y + h), (x, y + h)):
                add_path_point(*corner)
        elif op == "m" or op == "l":
            add_path_point(operands[0], operands[1])
        elif op == "c":
            for i in (0, 2, 4):
                add_path_point(operands[i], operands[i + 1])
        elif op == "v":
            add_path_point(operands[0], operands[1])
            add_path_point(operands[2], operands[3])
        elif op == "y":
            add_path_point(operands[0], operands[1])
            add_path_point(operands[2], operands[3])

        elif op in ("f", "F", "f*", "B", "B*", "b", "b*"):
            flush_fill(gstate.fill_color)
        elif op in ("S", "s", "n"):
            path_points = []
        elif op == "sh":
            background_rects.append(BackgroundRect(bbox=page_bbox, color=None))

        elif op == "Do":
            try:
                name = str(operands[0])
                xobj = resources.get("/XObject", Dictionary()).get(Name(name))
                subtype = str(xobj.get("/Subtype", "")) if xobj is not None else ""
            except Exception:
                subtype = ""
            corners = [apply_point(gstate.ctm, x, y) for x, y in ((0, 0), (1, 0), (1, 1), (0, 1))]
            if subtype == "/Image":
                background_rects.append(BackgroundRect(bbox=_bbox_of(corners), color=None))
            # Form XObjects: not recursed into (see module docstring).
        elif op in ("BI", "EI", "ID"):
            corners = [apply_point(gstate.ctm, x, y) for x, y in ((0, 0), (1, 0), (1, 1), (0, 1))]
            background_rects.append(BackgroundRect(bbox=_bbox_of(corners), color=None))

        elif op == "BT":
            text_matrix = IDENTITY
            line_matrix = IDENTITY
        elif op == "Tm":
            m = tuple(_num(v) for v in operands)
            text_matrix = m
            line_matrix = m
        elif op == "Td":
            tx, ty = _num(operands[0]), _num(operands[1])
            line_matrix = mat_mult(translate(tx, ty), line_matrix)
            text_matrix = line_matrix
        elif op == "TD":
            tx, ty = _num(operands[0]), _num(operands[1])
            leading = -ty
            line_matrix = mat_mult(translate(tx, ty), line_matrix)
            text_matrix = line_matrix
        elif op == "T*":
            line_matrix = mat_mult(translate(0, -leading), line_matrix)
            text_matrix = line_matrix
        elif op == "TL":
            leading = _num(operands[0])
        elif op == "Tr":
            render_mode = int(operands[0])
        elif op == "Tf":
            try:
                font_name = str(operands[0])
                font_size = _num(operands[1])
                current_font_dict = fonts.get(Name(font_name)) if fonts else None
                current_font_simple = (
                    current_font_dict is None or str(current_font_dict.get("/Subtype", "")) != "/Type0"
                )
            except Exception:
                current_font_dict = None
                current_font_simple = True

        if op in ("'", '"'):
            line_matrix = mat_mult(translate(0, -leading), line_matrix)
            text_matrix = line_matrix

        if op in TEXT_SHOW_OPS:
            has_text = any(
                (isinstance(o, pikepdf.Array) and len(o) > 0) or isinstance(o, (pikepdf.String, str))
                for o in operands
            )
            if not has_text:
                continue
            if render_mode in (3, 7):
                continue  # invisible text (e.g. OCR text layer over a scan)

            origin = apply_point(mat_mult(text_matrix, gstate.ctm), 0, 0)
            color = gstate.stroke_color if render_mode == 1 else gstate.fill_color

            preview = _decode_text_preview(operands, current_font_simple)
            is_bold = _font_is_bold(current_font_dict)
            stats.checked += 1

            if color is None:
                stats.needs_review += 1
                stats.findings.append(ContrastFinding(
                    page=page_number, status="needs_review", ratio=None, threshold=None,
                    text_preview=preview, font_size=font_size, is_bold=is_bold,
                    reason="Text color uses a spot color, pattern, or other colorspace that "
                           "couldn't be reliably resolved to RGB.",
                ))
            else:
                kind, bg_color = _background_at(background_rects, *origin)
                if kind == "unknown":
                    stats.needs_review += 1
                    stats.findings.append(ContrastFinding(
                        page=page_number, status="needs_review", ratio=None, threshold=None,
                        text_preview=preview, font_size=font_size, is_bold=is_bold,
                        reason="Text sits over an image, shading, or pattern fill -- background "
                               "color could not be determined.",
                    ))
                else:
                    ratio = contrast_ratio(color, bg_color)
                    passed, threshold = _classify(ratio, font_size, is_bold)
                    if passed:
                        stats.passed += 1
                    else:
                        stats.failed += 1
                        stats.findings.append(ContrastFinding(
                            page=page_number, status="fail", ratio=round(ratio, 2),
                            threshold=threshold, text_preview=preview, font_size=font_size,
                            is_bold=is_bold,
                            reason="",
                        ))


def check_pdf(pdf: pikepdf.Pdf) -> ContrastStats:
    stats = ContrastStats()
    for i, page in enumerate(pdf.pages):
        check_page(pdf, page, i + 1, stats)
    return stats
