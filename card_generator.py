"""
card_generator.py
Takes a downloaded news photo + headline/source text and composites a
clean, consistent "news card" image (1080x1350, Instagram portrait ratio).
Pure Pillow â€” no AI involved, so it's deterministic and never garbles text.
"""
import colorsys
import os as _os
import platform as _platform
import random
import textwrap
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

CANVAS_W, CANVAS_H = 1080, 1350       # Instagram portrait
IMAGE_H = 900                          # legacy text-anchor boundary (hook slide) - headline/meta
                                        # positions are still computed off this value so the copy
                                        # doesn't move, even though the photo itself now runs
                                        # past it (see HOOK_BLACK_BAR_H below).
LOGO_SIZE = 140                        # brand logo badge, bottom-right corner
PANEL_H = CANVAS_H - IMAGE_H           # legacy text panel height (still used for sizing math)

# Hook slide: the solid-black footer is now shrunk down to just a slim
# strip sized to comfortably hold the logo, vertically centered inside
# it. Everything above that strip is photo (or generated background) -
# including the area that used to be solid-black panel behind the
# headline - so the headline/source text now sits directly over the
# photo instead of over a black field. Text positions themselves are
# unchanged (still computed from IMAGE_H/PANEL_H above); only how much
# of the canvas is "photo" vs. "true black" has changed.
HOOK_BLACK_BAR_H = 220                 # bottom black strip height (hook slide only)
HOOK_PHOTO_H = CANVAS_H - HOOK_BLACK_BAR_H  # actual photo/background height on the hook slide

BG_COLOR = (18, 18, 20)                # near-black panel background
TEXT_COLOR = (245, 245, 245)
MUTED_COLOR = (170, 170, 175)

# "BREAKING" badge - fixed red/white regardless of theme, so it always
# reads as an urgent, distinct signal instead of blending into whatever
# gradient/theme the rest of the card is using that day.
BREAKING_BG = (214, 30, 30)
BREAKING_TEXT = (255, 255, 255)

# Category -> gradient color pair, used when there's no source photo
# (either by choice, for visual variety, or as a fallback when an
# article has no usable image). These cards get an "ILLUSTRATIVE IMAGE"
# label since they're not tied to the actual story photo.
CATEGORY_GRADIENTS = {
    "POLITICS": ((35, 25, 60), (90, 40, 110)),
    "BUSINESS": ((15, 45, 40), (30, 110, 90)),
    "SPORTS": ((50, 20, 15), (150, 60, 30)),
    "TECH": ((10, 30, 55), (30, 90, 160)),
    "ENTERTAINMENT": ((55, 15, 45), (150, 40, 110)),
    "WORLD": ((20, 35, 50), (50, 100, 130)),
    "NEWS": ((30, 30, 35), (80, 80, 90)),
}

# Headline gradient + matching logo, bundled as a single theme so a batch
# picks ONE look and stays consistent across all its cards, rather than
# each card randomly getting a mismatched gradient/logo combo.
_ASSETS_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "assets")
HEADLINE_THEMES = [
    {
        "name": "silver",
        # "gradient" still drives the tag pill / highlight-box color (via
        # _pill_colors_from_theme and theme["gradient"][0]) - unchanged.
        # The actual headline TEXT for this theme (logo_black_white) is
        # forced to plain bright white below, via headline_color_mode.
        "gradient": ["#858489", "#e7e4ef", "#858489", "#b9b9b9", "#858489"],
        "logo": _os.path.join(_ASSETS_DIR, "logo_black_white.png"),
        "headline_color_mode": "white",
    },
    {
        "name": "bronze_gold",
        # Solid color (not a gradient) - all stops identical so the
        # headline renders as one flat bright gold instead of shading
        # dark/light across lines. Also drives the tag pill background
        # (via _pill_colors_from_theme) and the highlight-marker box.
        # Left as-is - this is the "keep it same" theme.
        "gradient": ["#fac47f", "#fac47f", "#fac47f", "#fac47f", "#fac47f"],
        "logo": _os.path.join(_ASSETS_DIR, "logo_golden.png"),
    },
    {
        "name": "warm_taupe",
        # "gradient" still drives the tag pill / highlight-box color -
        # unchanged. The headline TEXT for this theme (logo_silver) is a
        # fixed plain solid color (no gradient) - see headline_color_mode.
        "gradient": ["#8b806f", "#e8decc", "#8b806f", "#b3ae9a", "#8d8c88"],
        "logo": _os.path.join(_ASSETS_DIR, "logo_silver.png"),
        "headline_color_mode": "solid",
        "headline_solid_color": "#dbd2b4",
    },
]


def _pill_colors_from_theme(theme: dict) -> tuple:
    """Solid tag-pill background derived from the theme's gradient - so
    the pill matches this run's chosen headline/logo color instead of a
    fixed gray - plus a black/white text color picked for contrast."""
    stops = [_hex_to_rgb(h) for h in theme["gradient"]]
    avg = tuple(sum(c[i] for c in stops) // len(stops) for i in range(3))
    luminance = 0.299 * avg[0] + 0.587 * avg[1] + 0.114 * avg[2]
    text_color = (0, 0, 0) if luminance > 140 else (255, 255, 255)
    return avg, text_color


def _hex_to_rgb(hex_color: str) -> tuple:
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


# The default English fonts (Oblata, Runtime) have no rupee-sign glyph -
# PIL silently draws an empty box (.notdef) instead of erroring, so a
# missing rupee sign is easy to miss visually. Swap it for "Rs. "
# everywhere text is drawn rather than switching fonts.
def _sanitize_currency_symbols(text: str) -> str:
    if not text:
        return text
    return text.replace("\u20b9", "Rs. ")


def _photo_accent_color(img: Image.Image) -> str:
    """Samples a plain solid headline color from a photo's own dominant
    hue (used by the 'warm_taupe'/logo_silver theme instead of its fixed
    gradient). Averages the photo down to one RGB, then boosts
    saturation/lightness so it stays a legible bright color over the
    photo's darkened lower portion instead of a muddy literal average."""
    small = img.convert("RGB").resize((32, 32))
    pixels = list(small.getdata())
    n = len(pixels)
    r = sum(p[0] for p in pixels) / n
    g = sum(p[1] for p in pixels) / n
    b = sum(p[2] for p in pixels) / n
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    s = max(s, 0.55)
    l = min(max(l, 0.62), 0.82)
    r2, g2, b2 = colorsys.hls_to_rgb(h, l, s)
    return "#%02x%02x%02x" % (int(r2 * 255), int(g2 * 255), int(b2 * 255))


# --- fonts -------------------------------------------------------------
# All 5 font files that ship in fonts/ are now wired in:
# Oblata Display (serif) is the default description/body font, and it's
# also the default hook headline font (matches the description slide).
# Tag pill / meta line stay on Runtime (clean sans, reads well small).
_FONT_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "fonts")
FONT_PLAYFAIR = _os.path.join(_FONT_DIR, "PlayfairDisplay-Bold.ttf")
FONT_RUNTIME = _os.path.join(_FONT_DIR, "RuntimeRegular-m2Odx.otf")
FONT_AVELINE = _os.path.join(_FONT_DIR, "AvelineEleganzaRegular-KVqeA.otf")
FONT_NORWAY = _os.path.join(_FONT_DIR, "Norway-rvVR7.ttf")
FONT_OBLATA = _os.path.join(_FONT_DIR, "OblataDisplayRegular-Zp8o8.otf")

# Kept around for callers that want the old random-per-card behavior
# (e.g. font_comparison.py), but it's no longer the default for new cards.
FONT_HEADLINE_CHOICES = [FONT_PLAYFAIR, FONT_RUNTIME, FONT_AVELINE, FONT_NORWAY, FONT_OBLATA]
FONT_HEADLINE = FONT_OBLATA  # default hook headline font - same as the description body font

FONT_TAG = FONT_RUNTIME
FONT_META = FONT_RUNTIME
FONT_BODY = FONT_OBLATA  # info-slide/description body text - MUST have working digit glyphs (real article text often has stats/dates) - verified Oblata Display has full digit + upper/lowercase coverage


# Fallback logo if a card is built without a theme (e.g. the self-test at
# the bottom of this file). Normal batch runs always pass a theme, which
# supplies the matching logo instead.
LOGO_PATH = _os.path.join(_ASSETS_DIR, "logo_silver.png")


_missing_font_warned = set()


def _load_font(path: str, size: int, variation: str = None) -> ImageFont.FreeTypeFont:
    try:
        font = ImageFont.truetype(path, size)
    except OSError:
        if path not in _missing_font_warned:
            print(f"[card_generator] WARNING: font not found at {path} - using a plain fallback font. "
                  f"Add the real font file there for correct typography.")
            _missing_font_warned.add(path)
        return ImageFont.load_default(size=size)
    if variation:
        try:
            font.set_variation_by_name(variation)
        except Exception:
            pass
    return font


def _tokenize_keep_phrase(text: str, keep_phrase: str = None) -> list:
    """Splits `text` on whitespace like str.split(), except that if
    `keep_phrase` (case-insensitive, whole-word match) appears in `text`,
    its words are glued into a single token so the wrapper below can
    never break a line in the middle of it.

    Without this, `_wrap_by_width`'s greedy word-wrap has no idea a
    highlight phrase exists and will happily split it across two lines
    whenever the phrase happens to straddle the wrap point - and
    `_find_highlight_bounds` requires the whole phrase on one line, so a
    split silently drops the highlighter-marker box entirely. Keeping
    the phrase atomic during wrapping is what makes the box reliably
    show up instead of only appearing when word-wrap happens to cooperate.
    """
    words = text.split()
    if not keep_phrase:
        return words
    phrase_words = keep_phrase.split()
    n = len(phrase_words)
    if n <= 1:
        return words
    tokens = []
    i = 0
    while i < len(words):
        if i + n <= len(words) and all(
            words[i + k].lower() == phrase_words[k].lower() for k in range(n)
        ):
            tokens.append(" ".join(words[i:i + n]))
            i += n
        else:
            tokens.append(words[i])
            i += 1
    return tokens


def _wrap_by_width(draw, text: str, font: ImageFont.FreeTypeFont, max_width: int,
                    keep_phrase: str = None) -> list:
    """Greedily wraps text into lines that actually fit max_width in
    pixels for the given font, instead of textwrap's fixed character
    count (which under- or over-fills the line depending on font/size).

    keep_phrase: if given, glues that phrase's words into one unbreakable
    token first (see _tokenize_keep_phrase) so it's never split across
    two lines - used to keep a highlight phrase intact for the box drawn
    behind it."""
    words = _tokenize_keep_phrase(text, keep_phrase)
    if not words:
        return []
    lines = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _autofit_text(draw, text: str, font_path: str, max_width: int, max_height: int,
                   max_size: int, min_size: int = 28, variation: str = None,
                   line_spacing_extra: int = 18, step: int = 2, side_margin: int = 0,
                   keep_phrase: str = None, extra_fit_check=None):
    """
    Picks the LARGEST font size (within [min_size, max_size]) whose
    pixel-wrapped text fits inside max_width x max_height. This makes
    short copy render bigger (filling the box) and long copy render
    smaller (fitting the box) instead of one fixed size that leaves
    empty space for short text and gets truncated for long text.

    `side_margin` reserves that many pixels on EACH side of the box that
    wrapping is not allowed to use - i.e. wrapping targets
    (max_width - 2*side_margin), while `max_width` itself stays the box
    width used for centering elsewhere. Without this, `_wrap_by_width`
    is free to pack a line right up to max_width, so a line can end up
    glued flush to the box's edges with zero breathing room while other
    lines in the same block sit comfortably inset - an uneven, "squeezed"
    look. With a margin reserved, every line - short or long - keeps at
    least side_margin px of clear space on both sides.

    `extra_fit_check`, if given, is called as extra_fit_check(lines, font,
    line_h) for each candidate size that already fits max_height, and
    must return True for that size to be accepted - otherwise the search
    keeps stepping down. This lets a caller reject a candidate for a
    reason other than raw height (e.g. the wrapped block would collide
    with a fixed element like a logo) WITHOUT shrinking max_height itself
    - shrinking max_height changes the width-driven word-wrap not at all,
    but can jump the chosen size down a lot more than needed and even
    flip the line count in unexpected directions. Checking the same
    width-based wrap at each size in turn keeps line breaks predictable
    as size decreases.

    Returns (font, wrapped_lines, line_height). If even min_size doesn't
    fit, the text is truncated with an ellipsis as a last resort.
    """
    wrap_width = max(1, max_width - 2 * side_margin)
    for size in range(max_size, min_size - 1, -step):
        font = _load_font(font_path, size, variation)
        lines = _wrap_by_width(draw, text, font, wrap_width, keep_phrase=keep_phrase)
        ascent, descent = font.getmetrics()
        line_h = ascent + descent + line_spacing_extra
        if line_h * len(lines) <= max_height:
            if extra_fit_check is None or extra_fit_check(lines, font, line_h):
                return font, lines, line_h

    font = _load_font(font_path, min_size, variation)
    lines = _wrap_by_width(draw, text, font, wrap_width, keep_phrase=keep_phrase)
    ascent, descent = font.getmetrics()
    line_h = ascent + descent + line_spacing_extra
    max_lines = max(1, max_height // line_h)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip() + "â€¦"
    return font, lines, line_h





def crop_to_fill(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Center-crop an image to exactly fill target_w x target_h (like CSS object-fit: cover)."""
    return ImageOps.fit(img, (target_w, target_h), method=Image.LANCZOS, centering=(0.5, 0.4))


def generate_gradient_background(width: int, height: int, tag: str = "NEWS") -> Image.Image:
    """
    Pure-code abstract background, colored by category, used when we're
    deliberately skipping the source photo (or one wasn't available).
    Deterministic and text-free by construction - no AI, no hallucination risk.
    """
    top_color, bottom_color = CATEGORY_GRADIENTS.get(tag.upper(), CATEGORY_GRADIENTS["NEWS"])

    img = Image.new("RGB", (width, height), top_color)
    draw = ImageDraw.Draw(img)
    for y in range(height):
        ratio = y / height
        r = int(top_color[0] + (bottom_color[0] - top_color[0]) * ratio)
        g = int(top_color[1] + (bottom_color[1] - top_color[1]) * ratio)
        b = int(top_color[2] + (bottom_color[2] - top_color[2]) * ratio)
        draw.line([(0, y), (width, y)], fill=(r, g, b))

    # A few soft, randomly placed circles for visual texture (subtle, on-brand)
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    for _ in range(4):
        cx, cy = random.randint(0, width), random.randint(0, int(height * 0.7))
        radius = random.randint(120, 320)
        alpha = random.randint(15, 35)
        overlay_draw.ellipse(
            [cx - radius, cy - radius, cx + radius, cy + radius],
            fill=(255, 255, 255, alpha),
        )
    overlay = overlay.filter(ImageFilter.GaussianBlur(80))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    return img


def _make_linear_gradient(width: int, height: int, hex_stops: list) -> Image.Image:
    """Vertical (180Â°) multi-stop linear gradient image, width x height."""
    stops = [_hex_to_rgb(h) for h in hex_stops]
    n = len(stops) - 1
    img = Image.new("RGB", (1, height))
    for y in range(height):
        pos = (y / max(height - 1, 1)) * n
        idx = min(int(pos), n - 1)
        local_t = pos - idx
        c0, c1 = stops[idx], stops[idx + 1]
        r = int(c0[0] + (c1[0] - c0[0]) * local_t)
        g = int(c0[1] + (c1[1] - c0[1]) * local_t)
        b = int(c0[2] + (c1[2] - c0[2]) * local_t)
        img.putpixel((0, y), (r, g, b))
    return img.resize((width, height))


def _find_highlight_bounds(draw, wrapped: list, font, highlight: str):
    """
    Finds `highlight` as a case-insensitive substring of exactly one of the
    already-wrapped headline lines. Returns a dict with everything needed
    to draw both the marker box and the overlaid solid-ink text (line
    index, this line's centered x-start, the highlighted glyph's pixel
    x-offset/width within it, its actual vertical ink extent, and the
    exact original-case slice actually being highlighted) - or None if
    not found in any single line (e.g. word-wrap happened to split the
    phrase across two lines).
    """
    if not highlight:
        return None
    highlight_norm = highlight.strip().lower()
    if not highlight_norm:
        return None

    for i, line in enumerate(wrapped):
        idx = line.lower().find(highlight_norm)
        if idx == -1:
            continue

        line_w = draw.textlength(line, font=font)

        prefix = line[:idx]
        # textlength (not textbbox) - bbox only measures visible ink, so a
        # trailing space in `prefix` (there always is one here, since the
        # highlight starts mid-line after a preceding word) would measure
        # as zero-width and the box would creep back into that space -
        # textlength uses the actual glyph advance width instead.
        prefix_w = draw.textlength(prefix, font=font) if prefix else 0

        # Use the ORIGINAL-case slice from the line (not `highlight` itself)
        # so glyph widths/rendering match exactly what's actually drawn.
        exact_slice = line[idx: idx + len(highlight_norm)]
        hl_w = draw.textlength(exact_slice, font=font)
        # top/bottom are the SLICE's actual ink extent (not the font's full
        # ascent+descent line-box, which is padded well beyond the ink -
        # especially for Devanagari, whose metrics reserve headroom for
        # tall matras/reph marks that may not even be present in this
        # particular phrase). Sizing the box off this instead of
        # line_height keeps it hugging the visible letters.
        hl_bbox = draw.textbbox((0, 0), exact_slice, font=font)
        hl_top, hl_bottom = hl_bbox[1], hl_bbox[3]

        return {
            "line_index": i, "line_w": line_w,
            "prefix_w": prefix_w, "hl_w": hl_w, "exact_slice": exact_slice,
            "hl_top": hl_top, "hl_bottom": hl_bottom,
        }
    return None


def _highlight_on_backdrop_line(bounds: dict, wrapped: list) -> bool:
    """True if the highlighted phrase sits on the headline's TOP line
    while that same line also carries the solid/translucent black
    backdrop box from _draw_top_line_backdrop (drawn whenever the
    headline wraps to 2+ lines - see that function). In that case the
    highlighter box/ink is skipped entirely and the phrase just renders
    in the headline's normal color - stacking the highlight marker on
    top of the backdrop box reads as a muddy double-layer instead of a
    clean highlighter mark, since the backdrop already darkens
    everything on that line, highlighted or not."""
    return bool(bounds) and len(wrapped) > 1 and bounds["line_index"] == 0


def _draw_highlight_box(canvas: Image.Image, bounds: dict, line_height: int, text_y: int,
                         pad_x: int, max_text_width: int, box_color: tuple, font_size: int,
                         font: ImageFont.FreeTypeFont = None, opacity: float = 0.6):
    """
    Paints a sharp-cornered "highlighter marker" box on the canvas at the
    position described by `bounds` (from _find_highlight_bounds), tinted
    with `box_color` (meant to be the headline's own gradient color, e.g.
    via _hex_to_rgb(theme["gradient"][0])). Must be called BEFORE
    _draw_gradient_text, since that function pastes gradient text using a
    per-glyph mask - anything already on the canvas behind the glyphs
    (this box) stays visible in the gaps around/behind the letters.

    opacity: 0-1 alpha for the box (0.6 = 60% opaque, i.e. the photo/
    headline panel underneath still shows through at 40% strength) - the
    canvas is plain RGB (no alpha channel), so this is done by cropping
    the box region and alpha-blending it against a solid box_color layer,
    the same technique _draw_top_line_backdrop uses, rather than a flat
    opaque fill - a real highlighter is translucent, not a solid block.

    Vertically sized off the highlighted slice's own ink bbox (hl_top/
    hl_bottom), not the line's full font-metric height - see
    _find_highlight_bounds.
    """
    i = bounds["line_index"]
    line_x = pad_x + (max_text_width - bounds["line_w"]) // 2  # matches _draw_gradient_text's centering
    # The box is sized tight to the highlighted text's own ink bounds
    # (no horizontal padding added/subtracted) - the natural word-space
    # already present in the line before/after the highlighted phrase
    # (exactly one space-character's width, since it's real text layout)
    # is left untouched outside the box, so that's the visible gap to
    # "on"/"for" etc. on each side.
    h_pad = 0
    # v_pad formula matches _draw_top_line_backdrop's exactly (max(3,
    # round(font_size * 0.04))) so both boxes read as the same visual
    # "thickness" around their text - they used to use different
    # formulas (this one was max(8, round(font_size * 0.10))), which
    # made the highlighter box noticeably chunkier than the backdrop box.
    v_pad = max(3, round(font_size * 0.04))
    line_top = text_y + i * line_height
    box = [
        line_x + bounds["prefix_w"] - h_pad,
        line_top + bounds["hl_top"] - v_pad,
        line_x + bounds["prefix_w"] + bounds["hl_w"] + h_pad,
        line_top + bounds["hl_bottom"] + v_pad,
    ]
    box = [
        int(max(0, box[0])), int(max(0, box[1])),
        int(min(canvas.width, box[2])), int(min(canvas.height, box[3])),
    ]
    if box[2] <= box[0] or box[3] <= box[1]:
        return
    region = canvas.crop(box)
    color_layer = Image.new("RGB", region.size, box_color)
    blended = Image.blend(region, color_layer, opacity)
    canvas.paste(blended, box)


def _draw_highlight_ink(canvas: Image.Image, bounds: dict, font, line_height: int, text_y: int,
                         pad_x: int, max_text_width: int, gradient_stops: list):
    """
    Redraws just the highlighted phrase on top of the (already-drawn)
    gradient headline text, using the SAME hex_stops as the rest of the
    headline (whatever this carousel's color_mode resolved to - gradient,
    flat white, flat solid, or the photo-sampled accent) rather than a
    fixed black. _make_linear_gradient's gradient is purely vertical (each
    row is one flat color, stretched across the full width - see its
    docstring), so building a fresh one sized to just this slice produces
    the exact same per-row color the full-line gradient would have shown
    through the same y-range; no need to crop from the full line's
    gradient. Must be called AFTER _draw_gradient_text so this ink isn't
    itself overpainted.
    """
    i = bounds["line_index"]
    line_x = pad_x + (max_text_width - bounds["line_w"]) // 2
    slice_w = int(round(bounds["hl_w"]))
    gradient_slice = _make_linear_gradient(slice_w + 4, line_height + 4, gradient_stops)
    mask = Image.new("L", (slice_w + 4, line_height + 4), 0)
    ImageDraw.Draw(mask).text((0, 0), bounds["exact_slice"], font=font, fill=255)
    paste_xy = (int(round(line_x + bounds["prefix_w"])), int(round(text_y + i * line_height)))
    canvas.paste(gradient_slice, paste_xy, mask)


def _draw_top_line_backdrop(canvas: Image.Image, wrapped: list, font: ImageFont.FreeTypeFont,
                             line_height: int, text_y: int, pad_x: int, max_text_width: int,
                             opacity: float = 0.4):
    """
    When the headline wraps to 2+ lines, the topmost line sits higher in
    the photo panel where the bottom-fade (see build_news_card) hasn't
    darkened the photo much yet, so it can run low-contrast against a
    busy/bright photo. This paints a black, sharp-cornered backdrop box
    behind just that top line (sized to its own text width, not a full-
    width bar) so it stays legible. Lower lines already sit on darker,
    more-faded photo and don't get one. No-op for single-line headlines.
    Must be called BEFORE the headline text itself is drawn, so the text
    paints on top of this box (mirrors _draw_highlight_box's draw-order
    requirement).

    opacity: 0-1 alpha for the black box (0.4 = 40% opaque, i.e. the
    photo underneath still shows through at 60% strength) - the canvas
    is plain RGB (no alpha channel), so this is done by cropping the
    box region and alpha-blending it against a solid black layer rather
    than a simple flat-fill rectangle.
    """
    if len(wrapped) < 2:
        return
    draw = ImageDraw.Draw(canvas)
    first_line = wrapped[0]
    bbox = draw.textbbox((0, 0), first_line, font=font)
    line_w, ink_top, ink_bottom = bbox[2], bbox[1], bbox[3]
    line_x = pad_x + (max_text_width - line_w) // 2
    h_pad = max(6, round(font.size * 0.08))
    v_pad = max(3, round(font.size * 0.04))
    box = [
        line_x - h_pad,
        text_y + ink_top - v_pad,
        line_x + line_w + h_pad,
        text_y + ink_bottom + v_pad,
    ]
    # Clamp to canvas bounds before cropping - textbbox/padding can in
    # principle push a coordinate slightly outside the image.
    box = [
        int(max(0, box[0])), int(max(0, box[1])),
        int(min(canvas.width, box[2])), int(min(canvas.height, box[3])),
    ]
    if box[2] <= box[0] or box[3] <= box[1]:
        return
    region = canvas.crop(box)
    black_layer = Image.new("RGB", region.size, (0, 0, 0))
    blended = Image.blend(region, black_layer, opacity)
    canvas.paste(blended, box)


def _draw_gradient_text(canvas: Image.Image, xy, lines, font, line_height, hex_stops, block_width=None, center=False):
    """
    Renders wrapped text lines filled with a vertical gradient (sampled
    from the card's full text-block height so the gradient flows smoothly
    across all lines, not repeated per-line).

    block_width: the width to center lines within (e.g. the panel's usable
    width). Required if center=True.
    center: if True, each line is horizontally centered within block_width
    independently (typical centered-headline look) rather than all lines
    starting flush at x.
    """
    x, y = xy
    draw = ImageDraw.Draw(canvas)
    line_widths = [draw.textbbox((0, 0), line, font=font)[2] for line in lines]
    max_w = max(line_widths, default=1)

    # Gradient is rendered once per line's own height (not stretched over
    # the whole block) so every line gets the full dark->light->dark
    # shimmer sweep evenly, instead of later lines landing on whatever
    # darker stop happens to fall at their position in a block-wide
    # gradient.
    line_gradient = _make_linear_gradient(max_w + 4, line_height + 4, hex_stops)

    for i, (line, line_w) in enumerate(zip(lines, line_widths)):
        mask = Image.new("L", (line_w + 4, line_height + 4), 0)
        ImageDraw.Draw(mask).text((0, 0), line, font=font, fill=255)
        gradient_slice = line_gradient.crop((0, 0, line_w + 4, line_height + 4))
        line_x = x + ((block_width - line_w) // 2 if center and block_width else 0)
        canvas.paste(gradient_slice, (line_x, y + i * line_height), mask)



def apply_duotone(img: Image.Image, dark_hex: str, light_hex: str) -> Image.Image:
    """
    Maps a photo's luminance onto a two-color gradient (dark shadows ->
    light highlights). Used on info-slide photos so slides 2+ are
    visually distinct from the hook slide while still being the real,
    honestly-sourced article photo - just re-colored, not a different image.
    """
    grayscale = ImageOps.grayscale(img)
    dark_rgb = _hex_to_rgb(dark_hex)
    light_rgb = _hex_to_rgb(light_hex)
    return ImageOps.colorize(grayscale, black=dark_rgb, white=light_rgb).convert("RGB")


# Duotone tint pairs used for info-slide photos, keyed the same as
# CATEGORY_GRADIENTS so the tint matches the story's category color.
DUOTONE_TINTS = {
    "POLITICS": ("#1a1330", "#c9a6e0"),
    "BUSINESS": ("#0a201c", "#8fe0c8"),
    "SPORTS": ("#251008", "#f2a878"),
    "TECH": ("#07172c", "#8fc4f2"),
    "ENTERTAINMENT": ("#26081c", "#f0a0d0"),
    "WORLD": ("#0f1c26", "#a0d0e8"),
    "NEWS": ("#1a1a1e", "#c8c8ce"),
}


def _draw_logo(canvas: Image.Image, pad_x: int, bottom_y: int, logo_size: int = 140, logo_path: str = None):
    """Paste the brand logo (rounded square) with its bottom edge at bottom_y."""
    logo_path = logo_path or LOGO_PATH
    if not _os.path.exists(logo_path):
        return
    logo = Image.open(logo_path).convert("RGBA").resize((logo_size, logo_size), Image.LANCZOS)
    mask = Image.new("L", (logo_size, logo_size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, logo_size, logo_size], radius=18, fill=255)
    logo_pos = (CANVAS_W - pad_x - logo_size, bottom_y - logo_size)
    canvas.paste(logo, logo_pos, mask)


def _draw_breaking_badge(draw: ImageDraw.ImageDraw, x: int, y: int, font: ImageFont.FreeTypeFont,
                          align: str = "left") -> list:
    """Draws a fixed red/white 'BREAKING' pill at (x, y).

    align="left" (default): (x, y) is the pill's top-left corner.
    align="right": x is instead the pill's desired RIGHT edge (e.g.
    CANVAS_W - pad_x) - the pill is drawn ending at x, growing leftward,
    so it sits flush against the right side of the card independent of
    its own text width. Used to put BREAKING in the top-right corner,
    opposite the category tag pill in the top-left (see build_news_card).

    Returns the pill's bounding box [x0, y0, x1, y1]."""
    text = "BREAKING"
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = 16
    box_w = w + pad * 2
    box_h = h + pad * 2
    x0 = (x - box_w) if align == "right" else x
    box = [x0, y, x0 + box_w, y + box_h]
    draw.rounded_rectangle(box, radius=8, fill=BREAKING_BG)
    draw.text((box[0] + pad, box[1] + pad - 4), text, font=font, fill=BREAKING_TEXT)
    return box


def build_news_card(
    photo_path: str,
    headline: str,
    source: str,
    tag: str = "NEWS",
    out_path: str = "news_card_output.png",
    slide_index: int = 0,
    total_slides: int = 1,
    theme: dict = None,
    headline_font: str = None,
    breaking: bool = False,
    grayscale: bool = False,
    highlight: str = None,
):
    theme = theme or random.choice(HEADLINE_THEMES)
    headline = _sanitize_currency_symbols(headline)
    source = _sanitize_currency_symbols(source)
    tag = _sanitize_currency_symbols(tag)
    # Hook headline font: defaults to the same font as the description slide
    # (FONT_BODY / Playfair) unless the caller pins a specific one, or passes
    # "random" to get the old per-card random pick from FONT_HEADLINE_CHOICES.
    if headline_font == "random":
        headline_font = random.choice(FONT_HEADLINE_CHOICES)
    else:
        headline_font = headline_font or FONT_BODY
    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), BG_COLOR)

    # --- top: photo (cropped to fill) or a generated gradient background ---
    # Runs the full HOOK_PHOTO_H now (almost the whole card, stopping only
    # at the slim black footer that holds the logo) - see HOOK_BLACK_BAR_H.
    is_generated_bg = photo_path is None
    if photo_path:
        photo = Image.open(photo_path).convert("RGB")
        photo = crop_to_fill(photo, CANVAS_W, HOOK_PHOTO_H)
    else:
        photo = generate_gradient_background(CANVAS_W, HOOK_PHOTO_H, tag=tag)

    # For serious/sensitive stories (deaths, sexual assault, murder, etc.)
    # the whole background - real photo or generated gradient alike -
    # is rendered as true black-and-white instead of the day's category
    # color, so these stories read as visually distinct and sober rather
    # than sharing the same bright/colorful treatment as routine news.
    if grayscale:
        photo = ImageOps.grayscale(photo).convert("RGB")

    # Sampled here (before the bottom-fade below darkens the photo) so an
    # "image" headline_color_mode reflects the actual photo's own color,
    # not the near-black gradient overlay composited over it further down.
    photo_accent_hex = (
        _photo_accent_color(photo)
        if theme.get("headline_color_mode") == "image" and not grayscale
        else None
    )

    # Bottom gradient fade so the photo darkens progressively behind the
    # headline/source text (which now sits directly over the photo, not
    # over a solid black panel) and blends cleanly into the black footer
    # strip at the very bottom. Fade starts near where the headline text
    # block begins (IMAGE_H, the legacy text anchor) and reaches full
    # black exactly at the top of the black footer strip.
    fade = Image.new("L", (CANVAS_W, HOOK_PHOTO_H), 0)
    fade_draw = ImageDraw.Draw(fade)
    fade_start = min(IMAGE_H, HOOK_PHOTO_H)
    fade_height = HOOK_PHOTO_H - fade_start
    for y in range(fade_height):
        alpha = int(255 * (y / fade_height))
        fade_draw.line([(0, fade_start + y), (CANVAS_W, fade_start + y)], fill=alpha)
    black_layer = Image.new("RGB", (CANVAS_W, HOOK_PHOTO_H), BG_COLOR)
    photo = Image.composite(black_layer, photo, fade)

    canvas.paste(photo, (0, 0))

    draw = ImageDraw.Draw(canvas)

    pad_x, pad_y = 40, 40

    # --- "Illustrative image" label when the photo isn't from the source article ---
    # This card's top image is a generated gradient, not tied to the actual story,
    # so viewers shouldn't mistake it for real photojournalism. Small, legible,
    # bottom-right corner of the image area, out of the way of the headline.
    if is_generated_bg:
        label_font = _load_font(FONT_META, 22)
        label_text = "ILLUSTRATIVE IMAGE"
        label_bbox = draw.textbbox((0, 0), label_text, font=label_font)
        label_w = label_bbox[2] - label_bbox[0]
        label_h = label_bbox[3] - label_bbox[1]
        label_pad = 10
        label_box = [
            CANVAS_W - pad_x - label_w - label_pad * 2,
            HOOK_PHOTO_H - 50 - label_h - label_pad * 2,
            CANVAS_W - pad_x,
            HOOK_PHOTO_H - 50,
        ]
        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rounded_rectangle(label_box, radius=6, fill=(0, 0, 0, 140))
        canvas.paste(Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB"), (0, 0))
        draw.text(
            (label_box[0] + label_pad, label_box[1] + label_pad - 4),
            label_text, font=label_font, fill=(255, 255, 255),
        )

    # --- BREAKING badge, only for stories flagged as breaking news - sits
    # in the top-RIGHT corner, opposite the category pill (top-left), so
    # the two never compete for the same space or overlap ---
    if breaking:
        breaking_font = _load_font(FONT_TAG, 30, variation="Bold")
        _draw_breaking_badge(draw, CANVAS_W - pad_x, pad_y, breaking_font, align="right")

    # --- tag pill (e.g. "NEWS", "TECH") - solid color pulled from this
    # batch's theme, so it matches the headline/logo instead of being a
    # fixed unrelated color. Always top-left, independent of BREAKING. ---
    tag_start_x = pad_x
    pill_bg, pill_text = ((235, 235, 235), (0, 0, 0)) if grayscale else _pill_colors_from_theme(theme)
    tag_font = _load_font(FONT_TAG, 30, variation="Bold")
    tag_bbox = draw.textbbox((0, 0), tag.upper(), font=tag_font)
    tag_w = tag_bbox[2] - tag_bbox[0]
    tag_h = tag_bbox[3] - tag_bbox[1]
    pill_pad = 18
    pill_box = [
        tag_start_x, pad_y,
        tag_start_x + tag_w + pill_pad * 2, pad_y + tag_h + pill_pad * 2,
    ]
    draw.rounded_rectangle(pill_box, radius=8, fill=pill_bg)
    draw.text(
        (pill_box[0] + pill_pad, pill_box[1] + pill_pad - 4),
        tag.upper(), font=tag_font, fill=pill_text,
    )

    # --- headline text (wrapped, auto-sized, gradient-filled) ---
    # Auto-fits within the panel between the photo and the source divider:
    # short headlines render bigger and fill the space, long ones shrink
    # to fit, and the block is vertically centered in that panel.
    #
    # The logo badge sits bottom-right, above the source line. It only
    # occupies a 140x140 corner, not the full width, so the fit check
    # below only rejects a candidate size when its LAST line would
    # actually run into that corner - most centered lines never reach
    # that far right, so most headlines fit against the full panel
    # height untouched. When a candidate does collide, the search just
    # steps down to the next smaller size (same width-driven word wrap,
    # so line breaks stay predictable) instead of shrinking the height
    # budget - shrinking the height was flipping 3 shorter lines into 2
    # much-wider, much-smaller ones to squeeze above the logo, which is
    # exactly the cramped look this is meant to avoid.
    max_text_width = CANVAS_W - 2 * pad_x
    panel_top = IMAGE_H + 45
    meta_y = CANVAS_H - 70
    logo_reserved_gap = 16
    # Logo now lives vertically centered inside the slim black footer
    # strip (HOOK_BLACK_BAR_H) instead of sitting low near the raw bottom
    # edge, so its top/left are derived from that strip.
    logo_top = (CANVAS_H - HOOK_BLACK_BAR_H) + (HOOK_BLACK_BAR_H - LOGO_SIZE) // 2
    logo_left = CANVAS_W - pad_x - LOGO_SIZE
    panel_bottom_full = CANVAS_H - 90  # top of the source-line divider
    available_h = panel_bottom_full - panel_top

    def _clears_logo(lines, font, line_h):
        if not (_os.path.exists(theme["logo"]) and lines):
            return True
        block_h = line_h * len(lines)
        text_y = panel_top + max(0, (available_h - block_h) // 2)
        last_line_bottom = text_y + block_h
        if last_line_bottom <= logo_top - logo_reserved_gap:
            return True
        last_line_bbox = draw.textbbox((0, 0), lines[-1], font=font)
        last_line_w = last_line_bbox[2] - last_line_bbox[0]
        last_line_x_end = pad_x + (max_text_width - last_line_w) // 2 + last_line_w
        return last_line_x_end <= logo_left - logo_reserved_gap

    headline_font_path = headline_font
    headline_font, wrapped, line_height = _autofit_text(
        draw, headline, headline_font_path, max_text_width, available_h,
        max_size=210, min_size=48, variation="Bold", line_spacing_extra=10,
        side_margin=24, keep_phrase=highlight, extra_fit_check=_clears_logo,
    )
    block_h = line_height * len(wrapped)
    text_y = panel_top + max(0, (available_h - block_h) // 2)

    # _autofit_text steps the font size down in fixed increments, and word
    # wrap can jump from N to N+1 lines right at the step where a bigger
    # size would technically fit - so the chosen size sometimes leaves a
    # visible gap below available_h even though it's the largest that fits.
    # Stretch the line spacing (not the glyph size) to close that gap, so
    # the block actually fills the full panel instead of floating in it
    # with dead space.
    if len(wrapped) > 0:
        stretched_line_height = max(line_height, available_h // len(wrapped))
        line_height = stretched_line_height
        block_h = line_height * len(wrapped)
        text_y = panel_top + max(0, (available_h - block_h) // 2)

    # Grayscale cards get a plain white/light-gray headline instead of
    # the day's theme color, keeping the whole card black-and-white.
    # Otherwise, headline color follows the theme's headline_color_mode:
    # "white" -> flat plain white (logo_black_white theme), "image" ->
    # flat color sampled from this card's own photo (logo_silver theme),
    # unset/"gradient" -> the theme's normal gradient (logo_golden theme).
    color_mode = theme.get("headline_color_mode", "gradient")
    if grayscale:
        gradient_stops = ["#ffffff", "#e0e0e0", "#ffffff", "#f2f2f2", "#ffffff"]
    elif color_mode == "white":
        gradient_stops = ["#ffffff"] * 5
    elif color_mode == "image" and photo_accent_hex:
        gradient_stops = [photo_accent_hex] * 5
    elif color_mode == "solid":
        gradient_stops = [theme.get("headline_solid_color", "#ffffff")] * 5
    else:
        gradient_stops = theme["gradient"]

    # Highlighter-marker box + ink overlay behind/over the AI-picked
    # phrase, if any. Skipped for grayscale/sensitive cards - see
    # docstring. The box uses the headline's own color (theme["gradient"][0],
    # its dominant/anchor stop - repeats at the start/middle/end of every
    # theme's gradient, see HEADLINE_THEMES) rather than a fixed color, so
    # it always matches that card's headline. It's drawn before the
    # gradient text (so it shows through the gaps around the glyphs); the
    # ink overlay is redrawn after, in the same color/gradient as the rest
    # of the headline (see _draw_highlight_ink) rather than a fixed black.
    highlight_bounds = _find_highlight_bounds(draw, wrapped, headline_font, highlight) if highlight and not grayscale else None
    # Never draw the highlighter box/ink over the top-line black backdrop
    # (see _highlight_on_backdrop_line) - only text that's sitting
    # directly on the photo gets the highlighter treatment.
    if _highlight_on_backdrop_line(highlight_bounds, wrapped):
        highlight_bounds = None

    _draw_top_line_backdrop(canvas, wrapped, headline_font, line_height, text_y, pad_x, max_text_width)

    if highlight_bounds:
        box_color = _hex_to_rgb(theme["gradient"][0])
        _draw_highlight_box(canvas, highlight_bounds, line_height, text_y, pad_x, max_text_width, box_color, headline_font.size, font=headline_font)

    _draw_gradient_text(canvas, (pad_x, text_y), wrapped, headline_font, line_height, gradient_stops,
                         block_width=max_text_width, center=True)

    if highlight_bounds:
        # The gold theme (bronze_gold) keeps solid-black highlight ink on
        # purpose - black reads best against its own warm gold box.
        # Every other theme uses the headline's own color/gradient (see
        # _draw_highlight_ink) so the phrase doesn't stand out in black.
        highlight_ink_stops = ["#000000"] * 5 if theme.get("name") == "bronze_gold" else gradient_stops
        _draw_highlight_ink(canvas, highlight_bounds, headline_font, line_height, text_y, pad_x, max_text_width, highlight_ink_stops)

    # --- source / meta line above the black footer, and the brand logo ---
    # The logo is now vertically centered inside the slim black footer
    # strip (logo_top, computed above) rather than pinned to the raw
    # bottom edge. The divider/source line stays at its original position
    # (meta_y, unchanged) and still stops short of the logo's footprint
    # instead of running a full-width line straight past/under it.
    meta_font = _load_font(FONT_META, 26, variation="Bold")
    logo_bottom_y = logo_top + LOGO_SIZE
    logo_x_gap = 24  # breathing room between the line's end and the logo
    line_end_x = CANVAS_W - pad_x
    if _os.path.exists(theme["logo"]):
        line_end_x = (CANVAS_W - pad_x - LOGO_SIZE) - logo_x_gap
    draw.line([(pad_x, meta_y - 20), (line_end_x, meta_y - 20)], fill=(60, 60, 64), width=2)
    draw.text((pad_x, meta_y), source.upper(), font=meta_font, fill=MUTED_COLOR)

    # --- brand logo, vertically centered in the black footer strip ---
    if _os.path.exists(theme["logo"]):
        _draw_logo(canvas, pad_x, logo_bottom_y, logo_size=LOGO_SIZE,
                   logo_path=_os.path.join(_ASSETS_DIR, "logo_black_white.png") if grayscale else theme["logo"])

    canvas.save(out_path, "JPEG", quality=92)
    return out_path


INFO_IMAGE_H = 520   # photo area height on info slides (shorter than the hook slide's, to leave more room for body text)


def build_info_slide(
    photo_path: str,
    body_text: str,
    tag: str,
    slide_index: int,
    total_slides: int,
    out_path: str = "news_card_info_slide.png",
    tint_override: tuple = None,
    theme: dict = None,
    breaking: bool = False,
):
    """
    An informational carousel slide: real (or generated-fallback) photo
    up top - duotone-tinted so it reads as visually distinct from the
    hook slide rather than a repeat - then wrapped body copy pulled
    from the actual article text, evenly inset on all four sides.

    tint_override: optional (dark_hex, light_hex) pair overriding the
    category's default duotone color, e.g. ("#000000", "#ffffff") for a
    true black-and-white treatment instead of a category-colored tint.
    """
    body_text = _sanitize_currency_symbols(body_text)
    tag = _sanitize_currency_symbols(tag)
    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), BG_COLOR)
    theme = theme or random.choice(HEADLINE_THEMES)

    is_generated_bg = photo_path is None
    dark_hex, light_hex = tint_override if tint_override else DUOTONE_TINTS.get(tag.upper(), DUOTONE_TINTS["NEWS"])

    if photo_path:
        photo = Image.open(photo_path).convert("RGB")
        photo = crop_to_fill(photo, CANVAS_W, INFO_IMAGE_H)
        photo = apply_duotone(photo, dark_hex, light_hex)
    else:
        photo = generate_gradient_background(CANVAS_W, INFO_IMAGE_H, tag=tag)
        if tint_override:
            # A generated background should also go true black-and-white
            # for sensitive stories, not stay category-colored, so the
            # whole carousel (hook + info slides) reads consistently.
            photo = ImageOps.grayscale(photo).convert("RGB")

    # bottom fade so the photo blends into the text panel
    fade = Image.new("L", (CANVAS_W, INFO_IMAGE_H), 0)
    fade_draw = ImageDraw.Draw(fade)
    fade_height = 140
    for y in range(fade_height):
        alpha = int(255 * (y / fade_height))
        fade_draw.line([(0, INFO_IMAGE_H - fade_height + y), (CANVAS_W, INFO_IMAGE_H - fade_height + y)], fill=alpha)
    black_layer = Image.new("RGB", (CANVAS_W, INFO_IMAGE_H), BG_COLOR)
    photo = Image.composite(black_layer, photo, fade)
    canvas.paste(photo, (0, 0))

    draw = ImageDraw.Draw(canvas)
    pad_x, pad_y = 40, 40

    if is_generated_bg:
        label_font = _load_font(FONT_META, 22)
        label_text = "ILLUSTRATIVE IMAGE"
        label_bbox = draw.textbbox((0, 0), label_text, font=label_font)
        label_w = label_bbox[2] - label_bbox[0]
        label_h = label_bbox[3] - label_bbox[1]
        label_pad = 10
        label_box = [
            CANVAS_W - pad_x - label_w - label_pad * 2,
            INFO_IMAGE_H - 50 - label_h - label_pad * 2,
            CANVAS_W - pad_x,
            INFO_IMAGE_H - 50,
        ]
        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rounded_rectangle(label_box, radius=6, fill=(0, 0, 0, 140))
        canvas.paste(Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB"), (0, 0))
        draw.text(
            (label_box[0] + label_pad, label_box[1] + label_pad - 4),
            label_text, font=label_font, fill=(255, 255, 255),
        )

    # --- BREAKING badge, same rule as the hook slide: only for stories
    # flagged as breaking, sits in the top-RIGHT corner, opposite the
    # category pill (top-left) ---
    if breaking:
        breaking_font = _load_font(FONT_TAG, 26, variation="Bold")
        _draw_breaking_badge(draw, CANVAS_W - pad_x, pad_y, breaking_font, align="right")

    # --- tag pill, same theme-matched color as the hook slide, for visual continuity ---
    tag_start_x = pad_x
    pill_bg, pill_text = _pill_colors_from_theme(theme)
    tag_font = _load_font(FONT_TAG, 26, variation="Bold")
    tag_bbox = draw.textbbox((0, 0), tag.upper(), font=tag_font)
    tag_w, tag_h = tag_bbox[2] - tag_bbox[0], tag_bbox[3] - tag_bbox[1]
    pill_pad = 16
    pill_box = [tag_start_x, pad_y, tag_start_x + tag_w + pill_pad * 2, pad_y + tag_h + pill_pad * 2]
    draw.rounded_rectangle(pill_box, radius=8, fill=pill_bg)
    draw.text((pill_box[0] + pill_pad, pill_box[1] + pill_pad - 4), tag.upper(), font=tag_font, fill=pill_text)

    # --- body copy: real extracted article text ---
    # No heading on this slide - straight into the story text. Font size
    # auto-fits to the available panel: short copy renders bigger and
    # fills the space, long copy shrinks to fit instead of truncating.
    max_text_width = CANVAS_W - 2 * pad_x
    panel_top = INFO_IMAGE_H + pad_x
    panel_bottom = CANVAS_H - pad_x
    available_h = panel_bottom - panel_top
    body_font, body_wrapped, body_line_h = _autofit_text(
        draw, body_text, FONT_BODY, max_text_width, available_h,
        max_size=64, min_size=32, line_spacing_extra=18,
        side_margin=24,
    )
    body_block_h = body_line_h * len(body_wrapped)

    # Center the body block within the panel (equal breathing room top
    # and bottom instead of clinging to the top).
    text_y = panel_top + max(0, (panel_bottom - panel_top - body_block_h) // 2)

    for line in body_wrapped:
        line_w = draw.textbbox((0, 0), line, font=body_font)[2]
        line_x = pad_x + (max_text_width - line_w) // 2
        draw.text((line_x, text_y), line, font=body_font, fill=MUTED_COLOR)
        text_y += body_line_h

    # No logo on this slide by design - the full panel goes to story text.

    canvas.save(out_path, "JPEG", quality=92)
    return out_path


def _tracked_text_width(draw, text: str, font: ImageFont.FreeTypeFont, tracking: int) -> float:
    return sum(draw.textlength(ch, font=font) for ch in text) + tracking * max(0, len(text) - 1)


def _draw_tracked_center_text(draw, text: str, font: ImageFont.FreeTypeFont, cx: float, y: float,
                               fill, tracking: int = 0, shadow: tuple = None):
    """Draws `text` horizontally centered on `cx`, optionally with letter-
    tracking (extra pixels between glyphs, for small-caps eyebrow-style
    text) and/or a soft drop shadow (offset_x, offset_y, rgba) for
    legibility over a busy photo. Plain draw.text() doesn't support
    tracking, hence the per-glyph loop when tracking is non-zero."""
    if not tracking:
        bbox = draw.textbbox((0, 0), text, font=font)
        x = cx - (bbox[2] - bbox[0]) / 2 - bbox[0]
        if shadow:
            draw.text((x + shadow[0], y + shadow[1]), text, font=font, fill=shadow[2])
        draw.text((x, y), text, font=font, fill=fill)
        return
    total_w = _tracked_text_width(draw, text, font, tracking)
    x = cx - total_w / 2
    for ch in text:
        w = draw.textlength(ch, font=font)
        if shadow:
            draw.text((x + shadow[0], y + shadow[1]), ch, font=font, fill=shadow[2])
        draw.text((x, y), ch, font=font, fill=fill)
        x += w + tracking


def build_ultimate_hook_slide(
    photo_paths: list,
    out_path: str,
    theme: dict = None,
    brand_name: str = "TIMELY BROUGHT",
    headline: str = "TOP STORIES",
    subheading: str = None,
    story_count: int = None,
) -> str:
    """
    Builds the carousel's very FIRST slide: a 2x2 collage of this
    batch's own story hook photos, so a viewer gets a preview of
    everything in the carousel before swiping, instead of only seeing
    story #1's own hook slide. Sits ahead of every story's own
    hook+info slides in post order (see run_combined).

    photo_paths: the RAW hook photo for each story in this batch (the
    same photo_path build_carousel/build_news_card uses for that
    story's own hook slide), in carousel order. Designed around exactly
    4 (matching STORIES_PER_POST) for a clean 2x2 grid - if fewer than
    4 are available the existing ones repeat to fill the grid; extras
    beyond 4 are ignored. A missing/unreadable photo falls back to a
    generated gradient tile rather than failing the whole slide.

    theme: only used for the corner logo (theme["logo"]) - the collage
    itself is deliberately theme-agnostic (plain bright white text, no
    gradient) so it reads consistently regardless of which day's
    rotating headline theme is active for the story slides that follow.

    brand_name: small tracked-caps line at the top (e.g. "TIMELY
    BROUGHT" for the English page, "TIMELY SAMACHAR" for Hindi - the
    Hindi page's brand mark is deliberately kept in Latin script even
    on the Hindi carousel, see card_generator_hindi.build_ultimate_hook_slide).

    headline: short line across the middle of the collage (default
    "TOP STORIES").

    subheading: bottom line; defaults to "{n} stories, one swipe away"
    where n is story_count (or len(photo_paths), capped at 4) if not
    given explicitly.

    Background photos render at ~65% opacity (a light dark overlay, not
    the heavier scrim used on the hook/info story slides) so the
    collage stays vivid and photo-forward while keeping all text -
    brand name, headline, subheading - legible in plain bright white.
    """
    theme = theme or random.choice(HEADLINE_THEMES)
    n = story_count or min(len(photo_paths), 4) or 4
    subheading = subheading or f"{n} stories, one swipe away"

    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), BG_COLOR)

    # --- 2x2 collage of this batch's own story photos ---
    cols, rows = 2, 2
    tile_w, tile_h = CANVAS_W // cols, CANVAS_H // rows
    usable_photos = [p for p in (photo_paths or []) if p and _os.path.exists(p)]
    for i in range(4):
        photo = None
        if usable_photos:
            src = usable_photos[i % len(usable_photos)]
            try:
                photo = Image.open(src).convert("RGB")
                photo = crop_to_fill(photo, tile_w + 2, tile_h + 2)
            except Exception:
                photo = None
        if photo is None:
            photo = generate_gradient_background(tile_w + 2, tile_h + 2, tag="NEWS")
        r, c = divmod(i, cols)
        canvas.paste(photo, (c * tile_w, r * tile_h))

    # thin dividers between tiles for a clean "collage" edge
    draw = ImageDraw.Draw(canvas)
    draw.line([(tile_w, 0), (tile_w, CANVAS_H)], fill=BG_COLOR, width=3)
    draw.line([(0, tile_h), (CANVAS_W, tile_h)], fill=BG_COLOR, width=3)

    # --- scrim: background images sit at ~50% opacity (a heavier dark
    # overlay than the individual story slides use) so the collage stays
    # readable while the headline's own metallic gradient stays legible ---
    overlay = Image.new("RGBA", canvas.size, (12, 12, 14, 128))  # 128/255 ~= 50% dark -> ~50% image opacity
    canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay)
    draw = ImageDraw.Draw(canvas, "RGBA")

    pad_x = 40
    white = (255, 255, 255, 255)

    # --- brand name, top, bright, tracked small caps + hairline rule ---
    eyebrow_font = _load_font(FONT_TAG, 34, variation="Bold")
    _draw_tracked_center_text(draw, brand_name.upper(), eyebrow_font, CANVAS_W / 2, 55, white, tracking=7)
    draw.line([(90, 120), (CANVAS_W - 90, 120)], fill=(255, 255, 255, 190), width=1)

    # --- headline, centered vertically (nudged down from dead-center),
    # using the EXACT same color resolution a normal story's own hook
    # slide uses for THIS batch's theme (see build_news_card's
    # color_mode block) - not a hardcoded look. "silver" theme ->
    # flat bright white, "bronze_gold" -> flat bright gold, "warm_taupe"
    # -> its solid champagne color. Whichever theme this batch rotated
    # to is what the ultimate-hook headline shows too, same as every
    # other slide in the batch. Via _draw_gradient_text, same renderer
    # build_news_card uses. A soft black shadow is drawn first
    # (gradient-filled text has no built-in shadow support) for
    # legibility over a busy photo seam. ---
    color_mode = theme.get("headline_color_mode", "gradient")
    if color_mode == "white":
        headline_gradient_stops = ["#ffffff"] * 5
    elif color_mode == "solid":
        headline_gradient_stops = [theme.get("headline_solid_color", "#ffffff")] * 5
    else:
        headline_gradient_stops = theme["gradient"]

    headline_font, wrapped, line_h = _autofit_text(
        draw, headline, FONT_HEADLINE, CANVAS_W - 2 * pad_x, 260,
        max_size=100, min_size=48, variation="Bold", line_spacing_extra=10, side_margin=24,
    )
    block_h = line_h * len(wrapped)
    text_y = (CANVAS_H - block_h) // 2 + 70
    block_width = CANVAS_W - 2 * pad_x
    for i, line in enumerate(wrapped):
        bbox = draw.textbbox((0, 0), line, font=headline_font)
        line_w = bbox[2] - bbox[0]
        line_x = pad_x + (block_width - line_w) // 2 - bbox[0]
        draw.text((line_x + 2, text_y + i * line_h + 3), line, font=headline_font, fill=(0, 0, 0, 150))
    _draw_gradient_text(canvas, (pad_x, text_y), wrapped, headline_font, line_h, headline_gradient_stops,
                         block_width=block_width, center=True)

    # --- subheading, bottom, bright ---
    sub_font = _load_font(FONT_TAG, 36)
    _draw_tracked_center_text(draw, subheading, sub_font, CANVAS_W / 2, CANVAS_H - 95, white)

    canvas = canvas.convert("RGB")
    if _os.path.exists(theme["logo"]):
        _draw_logo(canvas, pad_x, CANVAS_H - 40, logo_size=100, logo_path=theme["logo"])

    canvas.save(out_path, "JPEG", quality=92)
    return out_path


def build_carousel(
    photo_path: str,
    headline: str,
    source: str,
    tag: str,
    slide_texts: list,
    out_dir: str,
    base_filename: str,
    grayscale: bool = False,
    theme: dict = None,
    breaking: bool = False,
    highlight: str = None,
) -> list:
    """
    Builds a full carousel: slide 1 is the eye-catching hook (photo +
    short headline, via build_news_card), slides 2..N are informational,
    using real article text and a duotone-tinted variant of the same
    photo (or a per-slide generated background if there's no photo).

    `slide_texts` is a list of body-text chunks, one per info slide
    (typically from article_extract.get_carousel_slide_texts) - each
    entry becomes its own slide. If empty, only the hook slide is built.

    grayscale: if True, info slides use a true black-and-white treatment
    instead of the category-colored duotone tint.

    theme: a dict from HEADLINE_THEMES (gradient + matching logo). Pass
    the SAME theme across every story in a batch so the whole 20-image
    post shares one consistent look, rather than each card randomly
    picking its own (which was mismatching logo colors to gradients).
    If not provided, a random theme is picked once for this carousel.

    breaking: if True, every slide gets a red "BREAKING" badge next to
    its category pill. Only pass True for stories actually flagged as
    breaking news - this should never appear on routine stories.

    highlight: an exact substring of `headline` to draw a highlighter-
    marker box behind on the hook slide (slide 1 only - info slides
    don't get one). Pass None to skip. Silently skipped (no crash) if
    it isn't found verbatim in `headline`, or if grayscale=True (a
    bright marker box would clash with the deliberately sober look
    grayscale is used for on sensitive stories).

    Returns the list of output file paths, in post order.
    """
    theme = theme or random.choice(HEADLINE_THEMES)
    _os.makedirs(out_dir, exist_ok=True)
    total_slides = 1 + len(slide_texts)
    paths = []

    hook_path = _os.path.join(out_dir, f"{base_filename}_1.jpg")
    build_news_card(
        photo_path=photo_path,
        headline=headline,
        source=source,
        tag=tag,
        out_path=hook_path,
        slide_index=0,
        total_slides=total_slides,
        theme=theme,
        breaking=breaking,
        grayscale=grayscale,
        highlight=highlight,
    )
    paths.append(hook_path)

    for i, body_text in enumerate(slide_texts, start=1):
        slide_path = _os.path.join(out_dir, f"{base_filename}_{i + 1}.jpg")
        build_info_slide(
            photo_path=photo_path,
            body_text=body_text,
            tag=tag,
            slide_index=i,
            total_slides=total_slides,
            out_path=slide_path,
            tint_override=("#000000", "#ffffff") if grayscale else None,
            theme=theme,
            breaking=breaking,
        )
        paths.append(slide_path)

    return paths


if __name__ == "__main__":
    import tempfile
    # Self-test with a synthetic *textured* placeholder (radial shading + noise)
    # standing in for a real photo - a flat color block would make the duotone
    # effect on info slides look like a flat rectangle, which isn't
    # representative of what a real downloaded article photo looks like.
    placeholder = Image.new("RGB", (1600, 1000))
    px = placeholder.load()
    for y in range(1000):
        for x in range(1600):
            dx, dy = (x / 1600 - 0.5), (y / 1000 - 0.5)
            shade = 90 + 70 * (dx * dx + dy * dy) + random.randint(-15, 15)
            shade = max(0, min(255, int(shade)))
            px[x, y] = (shade, shade + 8, shade + 20)
    placeholder_path = _os.path.join(tempfile.gettempdir(), "placeholder.png")
    placeholder.save(placeholder_path)
    build_news_card(
        placeholder_path,
        "Government announces new policy on renewable energy investment for 2027",
        "Reuters",
        tag="Business",
        out_path="sample_output.png",
    )
    print("Sample card generated.")

    sample_slides = [
        "The policy sets a target of 50 gigawatts of new solar and wind capacity by 2027, backed by a dedicated infrastructure fund and streamlined land-acquisition rules for developers.",
        "Industry groups welcomed the announcement but flagged concerns about grid capacity and the pace of transmission-line approvals, which have historically lagged behind generation targets.",
    ]
    build_carousel(
        placeholder_path,
        "Government announces new policy on renewable energy investment for 2027",
        "Reuters",
        tag="Business",
        slide_texts=sample_slides,
        out_dir="output",
        base_filename="sample_carousel",
    )
    print("Sample carousel generated (3 slides).")
