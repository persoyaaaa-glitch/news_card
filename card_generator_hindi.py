"""
card_generator_hindi.py
Hindi (Devanagari) version of card_generator.py — same "news card" layout
and logic (1080x1350 Instagram-portrait card, hook slide + info-slide
carousel), but the headline/body/tag/source copy is Hindi and only one
font family is used (Kalam has been removed - it wasn't reading well
on posted cards, so every Hindi card now renders in Eczar):

  - Eczar                  -> serif slab Devanagari + Latin face.
    Weights available: Regular, Medium, SemiBold, Bold, ExtraBold.

Eczar has full native Devanagari AND Latin glyph coverage, so
(unlike the old Inknut Antiqua / League Spartan setup) there's no
per-string script detection needed — it renders every string on the
card, Hindi or Latin alike.

Which family is "active" for a given card is passed in via the
`font_family` param ("eczar" - the only remaining choice) on build_news_card /
build_info_slide / build_carousel. Callers that want strict one-by-one
rotation across posts (e.g. hourly_run.py) should track that externally
(see `_next_font_family` in hourly_run.py, mirroring the existing
theme_rotation pattern) and pass the result in. If not passed, a random
family is picked once per carousel — fine for standalone/test runs.

IMPORTANT — Devanagari shaping: Devanagari needs OpenType shaping
(conjunct formation, matra reordering) or the glyphs come out in the
wrong order / unmerged. Pillow only does this through the "raqm" text
layout engine, so every font load below explicitly requests
`ImageFont.Layout.RAQM`. Plain `ImageFont.truetype(...)` (Pillow's
default "basic" layout) WILL render Hindi text incorrectly — do not
remove the layout_engine argument.

IMPORTANT — logo: hourly_run.py rotates a shared `theme` dict (gradient
+ logo) that comes from card_generator.HEADLINE_THEMES (the ENGLISH
module) and passes that same object into both the English AND Hindi
build calls, purely so the two languages' cards share a matching
gradient look for the same story. That means `theme["logo"]` here is
always one of the English page's three logo files — it is NEVER this
account's own logo. This file deliberately ignores theme["logo"] for
drawing purposes and always stamps HINDI_LOGO_PATH instead, so the
Hindi carousel always shows the Hindi account's own branding regardless
of which theme (gradient) got rotated in for a given post.
"""
import colorsys
import os as _os
import random
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps, features as _pil_features

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
# of the canvas is "photo" vs. "true black" has changed. Mirrors the
# English generator's layout exactly (see card_generator.py).
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
BREAKING_LABEL_HI = "ब्रेकिंग"

# Category -> gradient color pair, used when there's no source photo
# (either by choice, for visual variety, or as a fallback when an
# article has no usable image). These cards get a "सांकेतिक तस्वीर"
# (illustrative/representative image) label since they're not tied to
# the actual story photo. Keyed by the same canonical English category
# keys as the English generator, so callers can pass either generator
# the same `tag` value.
CATEGORY_GRADIENTS = {
    "POLITICS": ((35, 25, 60), (90, 40, 110)),
    "BUSINESS": ((15, 45, 40), (30, 110, 90)),
    "SPORTS": ((50, 20, 15), (150, 60, 30)),
    "TECH": ((10, 30, 55), (30, 90, 160)),
    "ENTERTAINMENT": ((55, 15, 45), (150, 40, 110)),
    "WORLD": ((20, 35, 50), (50, 100, 130)),
    "NEWS": ((30, 30, 35), (80, 80, 90)),
}

# Hindi display labels for the canonical category keys above. If `tag`
# passed to build_news_card / build_info_slide matches one of these keys
# (case-insensitive), the pill shows the Hindi translation. If it doesn't
# match (caller already passed localized text, e.g. a custom Hindi tag),
# the string is shown as-is — this dict is a convenience, not a requirement.
CATEGORY_LABELS_HI = {
    "POLITICS": "राजनीति",
    "BUSINESS": "व्यापार",
    "SPORTS": "खेल",
    "TECH": "तकनीक",
    "ENTERTAINMENT": "मनोरंजन",
    "WORLD": "विश्व",
    "NEWS": "समाचार",
}

ILLUSTRATIVE_LABEL_HI = "सांकेतिक तस्वीर"

# Headline gradient + matching logo, bundled as a single theme so a batch
# picks ONE look and stays consistent across all its cards, rather than
# each card randomly getting a mismatched gradient/logo combo. Identical
# palette to the English generator's themes so a Hindi and English batch
# of the same story can share a look.
#
# NOTE: the "logo" key in each entry below is kept only so this module's
# own default (`theme = theme or random.choice(HEADLINE_THEMES)`, used
# when build_carousel/build_news_card are called standalone/directly,
# e.g. the __main__ self-test at the bottom of this file) has something
# valid to fall back to. In the real pipeline, hourly_run.py always
# passes in the ENGLISH module's theme object instead (see the file
# docstring above) — and even then, the logo actually drawn on the card
# ALWAYS comes from HINDI_LOGO_PATH below, never from theme["logo"].
_ASSETS_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "assets")
# Golden is now the ONLY headline color - silver/warm_taupe removed
# (not just deprioritized) so every card, whatever calls this module,
# renders the same flat gold headline instead of rotating through
# looks.
HEADLINE_THEMES = [
    {
        "name": "bronze_gold",
        # Solid color (not a gradient) - all stops identical so the
        # headline renders as one flat bright gold instead of shading
        # dark/light across lines. Also drives the tag pill background
        # (via _pill_colors_from_theme) and the highlight-marker box.
        "gradient": ["#fac47f", "#fac47f", "#fac47f", "#fac47f", "#fac47f"],
        "logo": _os.path.join(_ASSETS_DIR, "logo_golden.png"),
    },
]

# Fixed brand color for the ultimate-hook slide's headline (see
# build_ultimate_hook_slide) - literally HEADLINE_THEMES[0]'s own flat
# gold gradient (the only Hindi theme - see above), same color a normal
# story's own hook slide already renders in.
GOLD_HEADLINE_GRADIENT = HEADLINE_THEMES[0]["gradient"]

# The Hindi account's own logo — always used for the bottom-right badge
# on every Hindi card, regardless of which gradient theme got rotated in
# and regardless of grayscale/sensitive treatment. Drop the file at
# assets/logo-hi.png (same assets/ folder the English logos live in —
# this is the news-card watermark logo, a SEPARATE file from the PWA's
# logo-hi.png used on the account-picker screen, even though the two
# happen to share a filename convention).
HINDI_LOGO_PATH = _os.path.join(_ASSETS_DIR, "logo-hi.png")


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


def _photo_accent_color(img: Image.Image) -> str:
    """Samples a plain solid headline color from a photo's own dominant
    hue (used by the 'warm_taupe'/logo_silver theme instead of its fixed
    gradient). Mirrors card_generator.py's helper of the same name so
    English/Hindi cards of the same story pick the same accent color."""
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


# --- fonts ---------------------------------------------------------------
# Only the two attached families are used anywhere in this file, and a
# whole card uses exactly ONE of them (see `font_family` param below) —
# rotation between the two happens across posts, never within one card.
_FONT_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "fonts_hindi")

# Per-family, per-role font file. Eczar covers Devanagari + Latin
# natively, so one file per role is enough — no separate Deva/Latin
# routing needed the way the old Inknut/League setup required.
FONT_FAMILIES = {
    "eczar": {
        # Eczar has a full weight range and declares both dev2 + legacy
        # deva script tables, so it shapes correctly (verified) unlike
        # Tiro Devanagari Hindi, which was dropped for this reason.
        #
        # Regular (400) is Eczar's lightest cut - its variable-font axis
        # only goes from 400-800, there's no Light/Thin instance below
        # Regular - so Regular is used for every role here, including
        # the headline (the heavier static weights - Eczar-SemiBold.ttf
        # / Eczar-Bold.ttf / Eczar-ExtraBold.ttf - are bundled in
        # fonts_hindi/ too, in case a bolder headline look is wanted
        # later, but Regular is what actually ships).
        "headline": _os.path.join(_FONT_DIR, "Eczar-Regular.ttf"),
        "body": _os.path.join(_FONT_DIR, "Eczar-Regular.ttf"),
        "tag": _os.path.join(_FONT_DIR, "Eczar-Regular.ttf"),
        "meta": _os.path.join(_FONT_DIR, "Eczar-Regular.ttf"),
    },
}

# Only one choice now that Kalam has been removed. Kept as a list (not a
# bare constant) so hourly_run.py's _next_font_family rotation helper and
# any standalone/random fallback here keep working unchanged - rotating
# over a single-item list just always returns "eczar".
FONT_FAMILY_CHOICES = ["eczar"]
DEFAULT_FONT_FAMILY = "eczar"


def _font_path(family: str, role: str) -> str:
    """Resolves a (family, role) pair to a font file, falling back to
    DEFAULT_FONT_FAMILY/"body" if either is unrecognized so a typo in a
    caller never hard-crashes card generation."""
    fam = FONT_FAMILIES.get(family, FONT_FAMILIES[DEFAULT_FONT_FAMILY])
    return fam.get(role, fam["body"])

# Fallback logo if a card is built without a theme (e.g. the self-test at
# the bottom of this file). Normal batch runs always pass a theme, which
# supplies the matching logo instead.
LOGO_PATH = _os.path.join(_ASSETS_DIR, "logo_silver.png")

if not _pil_features.check("raqm"):
    print(
        "[card_generator_hindi] WARNING: Pillow was built without libraqm. "
        "Devanagari text (conjuncts, matra reordering) will render "
        "incorrectly without it. Install a Pillow build with raqm support "
        "(e.g. `pip install --upgrade Pillow` on a system with libraqm-dev "
        "available) before generating real cards."
    )

_missing_font_warned = set()

def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    """Loads a font with the 'raqm' layout engine, which is required for
    correct Devanagari shaping (conjunct formation + matra reordering).
    Falls back to Pillow's basic-layout default font if the file is
    missing, purely so a missing-asset dev environment doesn't crash."""
    try:
        return ImageFont.truetype(path, size, layout_engine=ImageFont.Layout.RAQM)
    except OSError:
        if path not in _missing_font_warned:
            print(f"[card_generator_hindi] WARNING: font not found at {path} - using a plain fallback font. "
                  f"Add the real font file there for correct typography.")
            _missing_font_warned.add(path)
        return ImageFont.load_default(size=size)


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
    pixels for the given font, instead of a fixed character count (which
    under- or over-fills the line depending on font/size). Word-splitting
    on whitespace works the same way for Hindi as for English since
    Devanagari text is space-separated between words.

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


def _wrap_tokens_by_width(draw, tokens: list, font: ImageFont.FreeTypeFont, max_width: int) -> list:
    """Same greedy wrap as _wrap_by_width, but keeps each line as a list
    of tokens instead of a joined string, so a caller can move whole
    tokens between lines afterward (e.g. to rebalance a wrap point)
    without ever splitting a glued keep_phrase token apart."""
    if not tokens:
        return []
    lines = [[tokens[0]]]
    for token in tokens[1:]:
        candidate = lines[-1] + [token]
        if draw.textbbox((0, 0), " ".join(candidate), font=font)[2] <= max_width:
            lines[-1] = candidate
        else:
            lines.append([token])
    return lines


def _rebalance_last_line(draw, lines_tokens: list, font: ImageFont.FreeTypeFont,
                          wrap_width: int, safe_last_width: int) -> list:
    """Shifts tokens one at a time from the start of the last line to
    the end of the second-to-last line - same font size, same total
    line count - until the last line's rendered width fits within
    safe_last_width, or until doing so would overflow the previous
    line past wrap_width (at which point it stops; the caller decides
    what to fall back to). This only relocates the existing break
    point by a word or two instead of re-wrapping everything at a
    smaller size/width, so headlines that already fit comfortably
    don't need to shrink at all to clear the logo."""
    lines_tokens = [list(line) for line in lines_tokens]
    if len(lines_tokens) < 2:
        return lines_tokens
    while True:
        last_line = lines_tokens[-1]
        last_w = draw.textbbox((0, 0), " ".join(last_line), font=font)[2]
        if last_w <= safe_last_width or len(last_line) <= 1:
            break
        moved = last_line[0]
        candidate_prev = lines_tokens[-2] + [moved]
        if draw.textbbox((0, 0), " ".join(candidate_prev), font=font)[2] > wrap_width:
            break
        lines_tokens[-2] = candidate_prev
        lines_tokens[-1] = last_line[1:]
    return lines_tokens


def _autofit_text(draw, text: str, font_path: str, max_width: int, max_height: int,
                   max_size: int, min_size: int, line_spacing_extra: int, step: int = 2,
                   side_margin: int = 0, keep_phrase: str = None, extra_fit_check=None):
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
    with a fixed element like a logo) WITHOUT shrinking side_margin for
    every line just to keep one line short - side_margin narrows the
    wrap width for the WHOLE block, so using it to dodge a corner badge
    starves every other line of width it never needed to give up.
    Stepping the font size down instead keeps every line using the full
    available width at whatever size is chosen. Mirrors
    card_generator.py's implementation exactly.

    Returns (font, wrapped_lines, line_height). If even min_size doesn't
    fit, the text is truncated with an ellipsis as a last resort.
    """
    wrap_width = max(1, max_width - 2 * side_margin)
    for size in range(max_size, min_size - 1, -step):
        font = _load_font(font_path, size)
        lines = _wrap_by_width(draw, text, font, wrap_width, keep_phrase=keep_phrase)
        ascent, descent = font.getmetrics()
        line_h = ascent + descent + line_spacing_extra
        if line_h * len(lines) <= max_height:
            if extra_fit_check is None or extra_fit_check(lines, font, line_h):
                return font, lines, line_h

    font = _load_font(font_path, min_size)
    lines = _wrap_by_width(draw, text, font, wrap_width, keep_phrase=keep_phrase)
    ascent, descent = font.getmetrics()
    line_h = ascent + descent + line_spacing_extra
    max_lines = max(1, max_height // line_h)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip() + "…"
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
    """Vertical (180°) multi-stop linear gradient image, width x height."""
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
    Same approach as card_generator.py's version: finds `highlight` as a
    case-insensitive substring of exactly one wrapped line, and returns the
    geometry needed to draw both the marker box and the solid-ink overlay.
    Returns None if not found in any single line.

    NOTE (Devanagari shaping): matra reordering during OpenType shaping
    means this logical-string-position measurement can be very slightly
    off from true glyph edges for a phrase boundary that falls mid-
    conjunct. Fine for the common case (boundaries on clean word breaks);
    worth a visual spot-check if a box looks shifted.
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
        # trailing space in `prefix` would measure as zero-width and the
        # box would creep back into that space - textlength uses the
        # actual glyph advance width instead.
        prefix_w = draw.textlength(prefix, font=font) if prefix else 0

        exact_slice = line[idx: idx + len(highlight_norm)]
        hl_w = draw.textlength(exact_slice, font=font)
        # Actual ink extent of just this slice (not the font's full
        # ascent+descent line-box, which for Devanagari reserves headroom
        # for tall matras/reph marks well beyond what's in this specific
        # phrase) - sizing the box off this instead of line_height keeps
        # it hugging the visible letters instead of towering over them.
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
    while that same line also carries the translucent black backdrop box
    from _draw_top_line_backdrop (drawn whenever the headline wraps to
    2+ lines - see that function). In that case the highlighter box/ink
    is skipped entirely and the phrase just renders in the headline's
    normal color - stacking the highlight marker on top of the backdrop
    box reads as a muddy double-layer instead of a clean highlighter
    mark, since the backdrop already darkens everything on that line,
    highlighted or not. Mirrors card_generator.py's helper of the same
    name."""
    return bool(bounds) and len(wrapped) > 1 and bounds["line_index"] == 0


def _draw_highlight_box(canvas: Image.Image, bounds: dict, line_height: int, text_y: int,
                         pad_x: int, max_text_width: int, box_color: tuple, font_size: int,
                         font: ImageFont.FreeTypeFont = None, opacity: float = 0.6):
    """Paints the sharp-cornered highlighter-marker box, tinted with
    box_color (the headline's own gradient color). Must run BEFORE
    _draw_gradient_text. Vertically sized off the highlighted slice's own
    ink bbox (hl_top/hl_bottom), not the line's full font-metric height -
    see _find_highlight_bounds. Mirrors card_generator.py's helper of the
    same name exactly, including the translucent (not flat-opaque) box.

    opacity: 0-1 alpha for the box (0.6 = 60% opaque, i.e. the photo/
    headline panel underneath still shows through at 40% strength) -
    done via crop + alpha-blend against a solid box_color layer, same
    technique as _draw_top_line_backdrop, rather than a flat opaque fill.

    Sized tight to the highlighted text's own ink bounds (no extra
    padding) - the natural word-space already in the line before/after
    the phrase is left untouched outside the box, giving a one-space
    visible gap to neighboring words on each side."""
    i = bounds["line_index"]
    line_x = pad_x + (max_text_width - bounds["line_w"]) // 2
    h_pad = 0
    # v_pad formula matches _draw_top_line_backdrop's exactly (max(3,
    # round(font_size * 0.04))) so both boxes read as the same visual
    # "thickness" around their text - they used to use different
    # formulas (this one was max(8, round(font_size * 0.10))), which
    # made the highlighter box noticeably chunkier than the backdrop box.
    # Mirrors card_generator.py exactly.
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
    Redraws the highlighted phrase over the gradient text using the SAME
    hex_stops as the rest of the headline (not a fixed black) - see the
    English card_generator._draw_highlight_ink for why a fresh gradient
    sized to just this slice is equivalent to cropping from the full
    line's gradient (it's purely vertical/per-row). Must run AFTER
    _draw_gradient_text.
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
    behind just that top line (sized to its own text width, not a
    full-width bar) so it stays legible. Lower lines already sit on
    darker, more-faded photo and don't get one. No-op for single-line
    headlines. Must be called BEFORE the headline text itself is drawn.
    Mirrors card_generator.py's helper of the same name exactly, including
    the translucent (not flat-opaque) box.

    opacity: 0-1 alpha for the black box (0.4 = 40% opaque, i.e. the
    photo underneath still shows through at 60% strength) - the canvas is
    plain RGB (no alpha channel), so this is done by cropping the box
    region and alpha-blending it against a solid black layer rather than
    a flat-fill rectangle.
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


def _breaking_badge_width(draw: ImageDraw.ImageDraw, label: str, font: ImageFont.FreeTypeFont) -> int:
    """Measures how wide _draw_breaking_badge's pill will be for this
    label/font, without drawing anything - lets a caller right-align the
    badge (position it so its RIGHT edge lands at a fixed x) before the
    actual draw call, since PIL has no built-in right-anchored drawing."""
    bbox = draw.textbbox((0, 0), label, font=font)
    w = bbox[2] - bbox[0]
    return w + 16 * 2  # same `pad = 16` as _draw_breaking_badge


def _draw_breaking_badge(draw: ImageDraw.ImageDraw, x: int, y: int, label: str, font: ImageFont.FreeTypeFont) -> list:
    """Draws a fixed red/white breaking-news pill with its top-left corner
    at (x, y). Returns the pill's bounding box [x0, y0, x1, y1] so the
    caller can position the next element (e.g. the category tag) after it."""
    bbox = draw.textbbox((0, 0), label, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = 16
    box = [x, y, x + w + pad * 2, y + h + pad * 2]
    draw.rounded_rectangle(box, radius=8, fill=BREAKING_BG)
    draw.text((box[0] + pad, box[1] + pad - 4), label, font=font, fill=BREAKING_TEXT)
    return box


def _tag_label(tag: str) -> str:
    """Hindi display label for a category tag. Recognized canonical keys
    (POLITICS, BUSINESS, ...) are translated; anything else (a caller-
    supplied Hindi string, or a custom tag) is shown exactly as passed."""
    return CATEGORY_LABELS_HI.get(tag.upper(), tag)


def build_news_card(
    photo_path: str,
    headline: str,
    source: str,
    tag: str = "NEWS",
    out_path: str = "news_card_output.png",
    slide_index: int = 0,
    total_slides: int = 1,
    theme: dict = None,
    breaking: bool = False,
    breaking_label: str = BREAKING_LABEL_HI,
    grayscale: bool = False,
    font_family: str = None,
    highlight: str = None,
):
    """Same layout as the English generator's build_news_card, but all
    copy is expected to be Hindi. `tag` may be a canonical English key
    (translated automatically via CATEGORY_LABELS_HI) or an already-
    localized string.

    Note on `theme`: only its "gradient" is used here (for the headline
    color) — its "logo" is ignored. The bottom-right badge always comes
    from HINDI_LOGO_PATH, since `theme` in the real pipeline is actually
    the English module's theme object (see the file docstring).

    font_family: "eczar" (the only remaining family) - every text
    element on the card uses it (not just the headline). If omitted,
    it defaults to "eczar" anyway - the param is kept for signature
    compatibility with hourly_run.py's _next_font_family rotation."""
    theme = theme or random.choice(HEADLINE_THEMES)
    font_family = font_family or random.choice(FONT_FAMILY_CHOICES)
    headline_font_path = _font_path(font_family, "headline")
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
    # Mirrors card_generator.py's _photo_accent_color usage.
    photo_accent_hex = (
        _photo_accent_color(photo)
        if theme.get("headline_color_mode") == "image" and not grayscale
        else None
    )

    # Bottom gradient fade so the photo darkens progressively behind the
    # headline/source text (which now sits directly over the photo, not
    # over a solid black panel) and blends cleanly into the black footer
    # strip at the very bottom. Fade starts a bit ABOVE the headline
    # panel's top (panel_top is IMAGE_H + a small offset - see below) so
    # the enlarged headline text always sits on already-darkened photo,
    # even at its topmost line, and still reaches full black exactly at
    # the top of the black footer strip.
    fade = Image.new("L", (CANVAS_W, HOOK_PHOTO_H), 0)
    fade_draw = ImageDraw.Draw(fade)
    fade_start = min(IMAGE_H - 60, HOOK_PHOTO_H)
    fade_height = HOOK_PHOTO_H - fade_start
    for y in range(fade_height):
        alpha = int(255 * (y / fade_height))
        fade_draw.line([(0, fade_start + y), (CANVAS_W, fade_start + y)], fill=alpha)
    black_layer = Image.new("RGB", (CANVAS_W, HOOK_PHOTO_H), BG_COLOR)
    photo = Image.composite(black_layer, photo, fade)

    canvas.paste(photo, (0, 0))

    draw = ImageDraw.Draw(canvas)

    pad_x, pad_y = 40, 40

    # --- "illustrative image" label when the photo isn't from the source article ---
    if is_generated_bg:
        label_font = _load_font(_font_path(font_family, "meta"), 22)
        label_text = ILLUSTRATIVE_LABEL_HI
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
    # in the top-RIGHT corner (fixed red/white so it never blends into
    # the day's theme color), independent of the category pill which
    # always starts at the top-left regardless of whether breaking is on ---
    tag_start_x = pad_x
    if breaking:
        breaking_font = _load_font(_font_path(font_family, "tag"), 30)
        breaking_w = _breaking_badge_width(draw, breaking_label, breaking_font)
        breaking_x = CANVAS_W - pad_x - breaking_w
        _draw_breaking_badge(draw, breaking_x, pad_y, breaking_label, breaking_font)

    # --- tag pill (e.g. "समाचार", "तकनीक") - solid color pulled from this
    # batch's theme, so it matches the headline/logo instead of being a
    # fixed unrelated color ---
    tag_text = _tag_label(tag)
    pill_bg, pill_text = ((235, 235, 235), (0, 0, 0)) if grayscale else _pill_colors_from_theme(theme)
    tag_font = _load_font(_font_path(font_family, "tag"), 30)
    tag_bbox = draw.textbbox((0, 0), tag_text, font=tag_font)
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
        tag_text, font=tag_font, fill=pill_text,
    )

    # --- headline text (wrapped, auto-sized, gradient-filled) ---
    # Auto-fits within the panel between the photo and the source divider:
    # short headlines render bigger and fill the space, long ones shrink
    # to fit, and the block is vertically centered in that panel.
    #
    # The logo badge sits bottom-right, above the source line. It only
    # occupies a 140x140 corner, not the full width - so we don't reserve
    # its height across the whole panel up front. Instead: fit against the
    # FULL panel height first, then only if the actual last line's
    # rendered box would genuinely overlap the logo's corner, refit
    # against a height that avoids it.
    max_text_width = CANVAS_W - 2 * pad_x
    # Panel top pulled up close to IMAGE_H (was +45) to hand the headline
    # a bigger vertical box to autofit into - directly increases the
    # rendered font size on most headlines. The fade above was moved to
    # start earlier (IMAGE_H - 60) specifically so this bigger panel's
    # topmost line still lands on darkened photo, not raw brightness.
    panel_top = IMAGE_H + 5
    meta_y = CANVAS_H - 70
    logo_reserved_gap = 16
    # Logo now lives vertically centered inside the slim black footer
    # strip (HOOK_BLACK_BAR_H) instead of sitting low near the raw bottom
    # edge, so its top/left are derived from that strip - matches the
    # English generator's layout.
    logo_top = (CANVAS_H - HOOK_BLACK_BAR_H) + (HOOK_BLACK_BAR_H - LOGO_SIZE) // 2
    logo_left = CANVAS_W - pad_x - LOGO_SIZE
    panel_bottom_full = CANVAS_H - 90  # top of the source-line divider
    available_h = panel_bottom_full - panel_top

    # min_size/line_spacing bumped up slightly vs. the English generator:
    # Devanagari matras/conjuncts need a bit more vertical breathing room
    # and stop being legible sooner than Latin does as size shrinks.
    # Floor is 48, not 60: a longer headline can need one more line to
    # wrap into than a 60-min search would ever find room for, and a
    # 60-only search that fails just truncates with an ellipsis instead
    # of trying a smaller size that actually fits whole. 48 is chosen so
    # even a worst-case 3-line headline still fits available_h - most
    # headlines still land at 60+ since _autofit_text always tries the
    # largest working size first.
    headline_font, wrapped, line_height = _autofit_text(
        draw, headline, headline_font_path, max_text_width, available_h,
        max_size=190, min_size=48, line_spacing_extra=9, side_margin=16,
        keep_phrase=highlight,
    )
    block_h = line_height * len(wrapped)
    text_y = panel_top + max(0, (available_h - block_h) // 2)

    if _os.path.exists(HINDI_LOGO_PATH) and wrapped:
        last_line_bottom = text_y + line_height * len(wrapped)
        if last_line_bottom > logo_top - logo_reserved_gap:
            last_line_bbox = draw.textbbox((0, 0), wrapped[-1], font=headline_font)
            last_line_w = last_line_bbox[2] - last_line_bbox[0]
            last_line_x_start = pad_x + (max_text_width - last_line_w) // 2
            last_line_x_end = last_line_x_start + last_line_w
            # Only re-fit if the last line would actually reach into the
            # logo's horizontal corner too - most centered short lines
            # never get that far right.
            if last_line_x_end > logo_left - logo_reserved_gap:
                # Don't shrink the vertical box - the space above the
                # logo (~209px) can only ever hold ONE line at min_size,
                # so that always collapsed a legitimate 2-line headline
                # to 1 line + an ellipsis. The block only needs to dodge
                # the logo horizontally (it's a small bottom-right
                # corner badge, not a full-width bar).
                #
                # First choice: rebalance the existing wrap - move the
                # last line's leading word(s) back onto the previous
                # line, same font size, same total line count. This is
                # the minimal fix for the common case (a 2-line
                # headline with slack to spare) and doesn't force a 3rd
                # line the way re-wrapping everything at a narrower
                # width would (that just relocates the truncation
                # instead of fixing it, since 3 lines' worth of text at
                # min_size no longer fits available_h).
                wrap_width = max_text_width - 2 * 16  # matches the side_margin used above
                safe_last_line_w = 2 * (logo_left - logo_reserved_gap - pad_x) - max_text_width
                tokens = _tokenize_keep_phrase(headline, highlight)
                lines_tokens = _wrap_tokens_by_width(draw, tokens, headline_font, wrap_width)
                rebalanced_tokens = _rebalance_last_line(draw, lines_tokens, headline_font, wrap_width, safe_last_line_w)
                rebalanced = [" ".join(line) for line in rebalanced_tokens]
                rebalanced_last_bbox = draw.textbbox((0, 0), rebalanced[-1], font=headline_font) if rebalanced else None

                if rebalanced_last_bbox and (rebalanced_last_bbox[2] - rebalanced_last_bbox[0]) <= safe_last_line_w:
                    wrapped = rebalanced
                    block_h = line_height * len(wrapped)
                    text_y = panel_top + max(0, (available_h - block_h) // 2)
                else:
                    # Rebalancing alone couldn't clear it (previous line
                    # has no slack left, usually because the headline is
                    # long enough to need 3 lines to dodge the logo at
                    # all). Re-fit by stepping the font size DOWN instead
                    # of narrowing side_margin - side_margin narrows the
                    # wrap width for every line in the block, so using it
                    # here to shrink just the last line ends up starving
                    # every OTHER line of width it never needed to give
                    # up too (they'd wrap short and sit centered with a
                    # lot of unused space on both sides - the bug this
                    # replaced). extra_fit_check keeps side_margin at the
                    # original 16px for every candidate size and only
                    # accepts a size once its own last line naturally
                    # clears the logo corner at that size's own width, so
                    # every line - including the last - always uses as
                    # much of the panel's width as its font size allows.
                    def _clears_logo(lines, font, line_h):
                        if not lines:
                            return True
                        last_bbox = draw.textbbox((0, 0), lines[-1], font=font)
                        last_w = last_bbox[2] - last_bbox[0]
                        last_x_end = pad_x + (max_text_width - last_w) // 2 + last_w
                        return last_x_end <= logo_left - logo_reserved_gap

                    headline_font, wrapped, line_height = _autofit_text(
                        draw, headline, headline_font_path, max_text_width, available_h,
                        max_size=190, min_size=48, line_spacing_extra=9, side_margin=16,
                        keep_phrase=highlight, extra_fit_check=_clears_logo,
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
    # Mirrors card_generator.py exactly.
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

    highlight_bounds = _find_highlight_bounds(draw, wrapped, headline_font, highlight) if highlight and not grayscale else None
    # Never draw the highlighter box/ink over the top-line black backdrop
    # (see _highlight_on_backdrop_line) - only text that's sitting
    # directly on the photo gets the highlighter treatment. Mirrors
    # card_generator.py exactly.
    if _highlight_on_backdrop_line(highlight_bounds, wrapped):
        highlight_bounds = None

    _draw_top_line_backdrop(canvas, wrapped, headline_font, line_height, text_y, pad_x, max_text_width)

    if highlight_bounds:
        box_color = _hex_to_rgb(theme["gradient"][0])
        _draw_highlight_box(canvas, highlight_bounds, line_height, text_y, pad_x, max_text_width, box_color, headline_font.size, font=headline_font)

    _draw_gradient_text(canvas, (pad_x, text_y), wrapped, headline_font, line_height, gradient_stops,
                         block_width=max_text_width, center=True)

    if highlight_bounds:
        # Mirrors card_generator.py: bronze_gold keeps solid-black
        # highlight ink on purpose, every other theme matches its own
        # headline color/gradient instead of black.
        highlight_ink_stops = ["#000000"] * 5 if theme.get("name") == "bronze_gold" else gradient_stops
        _draw_highlight_ink(canvas, highlight_bounds, headline_font, line_height, text_y, pad_x, max_text_width, highlight_ink_stops)

    # --- source / meta line above the black footer, and the brand logo ---
    # The logo is now vertically centered inside the slim black footer
    # strip (logo_top, computed above) rather than pinned to the raw
    # bottom edge - matches the English generator's layout.
    meta_font = _load_font(_font_path(font_family, "meta"), 26)
    logo_bottom_y = logo_top + LOGO_SIZE
    logo_x_gap = 24  # breathing room between the line's end and the logo
    line_end_x = CANVAS_W - pad_x
    if _os.path.exists(HINDI_LOGO_PATH):
        line_end_x = (CANVAS_W - pad_x - LOGO_SIZE) - logo_x_gap
    draw.line([(pad_x, meta_y - 20), (line_end_x, meta_y - 20)], fill=(60, 60, 64), width=2)
    draw.text((pad_x, meta_y), source, font=meta_font, fill=MUTED_COLOR)

    # --- brand logo, bottom-right corner (square badge) ---
    # Always this account's own logo (see HINDI_LOGO_PATH / file docstring)
    # regardless of theme or grayscale/sensitive treatment - there's no
    # separate black-and-white variant of the Hindi logo, so sensitive
    # stories still show the same color badge rather than falling back
    # to the English page's B&W asset.
    if _os.path.exists(HINDI_LOGO_PATH):
        _draw_logo(canvas, pad_x, logo_bottom_y, logo_size=LOGO_SIZE, logo_path=HINDI_LOGO_PATH)

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
    breaking_label: str = BREAKING_LABEL_HI,
    font_family: str = None,
):
    """
    An informational carousel slide: real (or generated-fallback) photo
    up top - duotone-tinted so it reads as visually distinct from the
    hook slide rather than a repeat - then wrapped Hindi body copy pulled
    from the actual article text, evenly inset on all four sides.

    font_family: "eczar" (the only remaining family) - same param as
    build_news_card. Pass the SAME value used for this carousel's hook
    slide so all slides in one post share one font family.

    tint_override: optional (dark_hex, light_hex) pair overriding the
    category's default duotone color, e.g. ("#000000", "#ffffff") for a
    true black-and-white treatment instead of a category-colored tint.

    (No logo on info slides — same as the English generator, by design.)
    """
    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), BG_COLOR)
    theme = theme or random.choice(HEADLINE_THEMES)
    font_family = font_family or random.choice(FONT_FAMILY_CHOICES)

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
        label_font = _load_font(_font_path(font_family, "meta"), 22)
        label_text = ILLUSTRATIVE_LABEL_HI
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

    # --- BREAKING badge, same rule as the hook slide: top-right corner,
    # independent of the category pill (always top-left) ---
    tag_start_x = pad_x
    if breaking:
        breaking_font = _load_font(_font_path(font_family, "tag"), 26)
        breaking_w = _breaking_badge_width(draw, breaking_label, breaking_font)
        breaking_x = CANVAS_W - pad_x - breaking_w
        _draw_breaking_badge(draw, breaking_x, pad_y, breaking_label, breaking_font)

    # --- tag pill, same theme-matched color as the hook slide, for visual continuity ---
    tag_text = _tag_label(tag)
    pill_bg, pill_text = _pill_colors_from_theme(theme)
    tag_font = _load_font(_font_path(font_family, "tag"), 26)
    tag_bbox = draw.textbbox((0, 0), tag_text, font=tag_font)
    tag_w, tag_h = tag_bbox[2] - tag_bbox[0], tag_bbox[3] - tag_bbox[1]
    pill_pad = 16
    pill_box = [tag_start_x, pad_y, tag_start_x + tag_w + pill_pad * 2, pad_y + tag_h + pill_pad * 2]
    draw.rounded_rectangle(pill_box, radius=8, fill=pill_bg)
    draw.text((pill_box[0] + pill_pad, pill_box[1] + pill_pad - 4), tag_text, font=tag_font, fill=pill_text)

    # --- body copy: real extracted (translated) article text ---
    # No heading on this slide - straight into the story text. Font size
    # auto-fits to the available panel: short copy renders bigger and
    # fills the space, long copy shrinks to fit instead of truncating.
    max_text_width = CANVAS_W - 2 * pad_x
    panel_top = INFO_IMAGE_H + pad_x
    panel_bottom = CANVAS_H - pad_x
    available_h = panel_bottom - panel_top
    body_font, body_wrapped, body_line_h = _autofit_text(
        draw, body_text, _font_path(font_family, "body"), max_text_width, available_h,
        max_size=58, min_size=34, line_spacing_extra=24, side_margin=24,
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


def _draw_tracked_center_text(draw, text: str, font: ImageFont.FreeTypeFont, cx: float, y: float,
                               fill, tracking: int = 0, shadow: tuple = None):
    """Mirrors card_generator.py's helper of the same name. Per-glyph
    tracking is only ever used here for the LATIN brand name (e.g.
    "TIMELY SAMACHAR") - never pass tracking>0 for Devanagari text, since
    splitting a shaped Devanagari string into individual code points
    would break conjunct/matra shaping. Devanagari headline/subheading
    text always goes through the tracking=0 branch, which draws the
    whole string in one call so RAQM shapes it correctly."""
    if not tracking:
        bbox = draw.textbbox((0, 0), text, font=font)
        x = cx - (bbox[2] - bbox[0]) / 2 - bbox[0]
        if shadow:
            draw.text((x + shadow[0], y + shadow[1]), text, font=font, fill=shadow[2])
        draw.text((x, y), text, font=font, fill=fill)
        return
    total_w = sum(draw.textlength(ch, font=font) for ch in text) + tracking * max(0, len(text) - 1)
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
    font_family: str = DEFAULT_FONT_FAMILY,
    brand_name: str = "TIMELY SAMACHAR",
    headline: str = "हर बड़ी खबर",
    subheading: str = None,
    story_count: int = None,
) -> str:
    """
    Hindi mirror of card_generator.build_ultimate_hook_slide. Builds the
    carousel's very FIRST slide: a 2x2 collage of this batch's own story
    hook photos, so a viewer gets a preview of everything in the Hindi
    carousel before swiping.

    brand_name stays in LATIN script ("TIMELY SAMACHAR") even on the
    Hindi page by design - this is the page's brand mark, not body copy,
    so it's drawn with tracking like the English version rather than
    through the Devanagari shaping path.

    headline/subheading are Devanagari by default ("हर बड़ी खबर" /
    "{n} कहानियां, एक स्वाइप दूर") and always render through the
    RAQM-backed _load_font/_draw_tracked_center_text(tracking=0) path so
    conjuncts and matras shape correctly.

    theme is accepted for call-site symmetry with the English version
    but the corner logo ALWAYS comes from HINDI_LOGO_PATH, never
    theme["logo"] - same rule as every other slide in this file (see
    file docstring / build_news_card).
    """
    n = story_count or min(len(photo_paths), 4) or 4
    subheading = subheading or f"{n} कहानियां, एक स्वाइप दूर"

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

    draw = ImageDraw.Draw(canvas)
    draw.line([(tile_w, 0), (tile_w, CANVAS_H)], fill=BG_COLOR, width=3)
    draw.line([(0, tile_h), (CANVAS_W, tile_h)], fill=BG_COLOR, width=3)

    # --- scrim: background images sit at ~50% opacity, same as the
    # English version, so the collage stays readable while the
    # headline's own gold gradient stays legible ---
    overlay = Image.new("RGBA", canvas.size, (12, 12, 14, 128))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay)
    draw = ImageDraw.Draw(canvas, "RGBA")

    pad_x = 40
    gold = (250, 196, 127, 255)  # #fac47f - same flat gold as GOLD_HEADLINE_GRADIENT/HEADLINE_THEMES[0]

    # --- brand name, top, gold, tracked Latin small caps + gold hairline rule ---
    eyebrow_font = _load_font(_font_path(font_family, "tag"), 34)
    _draw_tracked_center_text(draw, brand_name.upper(), eyebrow_font, CANVAS_W / 2, 55, gold, tracking=7)
    draw.line([(90, 120), (CANVAS_W - 90, 120)], fill=(250, 196, 127, 210), width=1)

    # --- headline, centered vertically (nudged down from dead-center), in
    # the SAME fixed gold used for a normal Hindi story's own hook slide
    # (HEADLINE_THEMES[0]/"bronze_gold" - the only Hindi theme, see file
    # docstring) via _draw_gradient_text, same renderer build_carousel's
    # hook slide uses. A soft black shadow is drawn first (gradient-filled
    # text has no built-in shadow support) for legibility over a busy
    # photo seam. Devanagari-safe: no per-glyph tracking anywhere in this
    # block (see _draw_tracked_center_text's docstring for why), and
    # _draw_gradient_text draws each full line in one call, so RAQM shapes
    # conjuncts/matras correctly same as everywhere else in this file. ---
    headline_font, wrapped, line_h = _autofit_text(
        draw, headline, _font_path(font_family, "headline"), CANVAS_W - 2 * pad_x, 260,
        max_size=100, min_size=48, line_spacing_extra=10, side_margin=24,
    )
    block_h = line_h * len(wrapped)
    text_y = (CANVAS_H - block_h) // 2 + 70
    block_width = CANVAS_W - 2 * pad_x
    for i, line in enumerate(wrapped):
        bbox = draw.textbbox((0, 0), line, font=headline_font)
        line_w = bbox[2] - bbox[0]
        line_x = pad_x + (block_width - line_w) // 2 - bbox[0]
        draw.text((line_x + 2, text_y + i * line_h + 3), line, font=headline_font, fill=(0, 0, 0, 150))
    _draw_gradient_text(canvas, (pad_x, text_y), wrapped, headline_font, line_h, GOLD_HEADLINE_GRADIENT,
                         block_width=block_width, center=True)

    # --- subheading, bottom, gold, Devanagari-safe (no tracking) ---
    sub_font = _load_font(_font_path(font_family, "meta"), 36)
    _draw_tracked_center_text(draw, subheading, sub_font, CANVAS_W / 2, CANVAS_H - 95, gold)

    canvas = canvas.convert("RGB")
    # Always this page's own logo - never theme["logo"] - see file docstring.
    if _os.path.exists(HINDI_LOGO_PATH):
        _draw_logo(canvas, pad_x, CANVAS_H - 40, logo_size=100, logo_path=HINDI_LOGO_PATH)

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
    breaking_label: str = BREAKING_LABEL_HI,
    font_family: str = None,
    highlight: str = None,
) -> list:
    """
    Builds a full Hindi carousel: slide 1 is the eye-catching hook (photo +
    short Hindi headline, via build_news_card), slides 2..N are
    informational, using real (translated) article text and a duotone-
    tinted variant of the same photo (or a per-slide generated background
    if there's no photo).

    font_family: "eczar" (the only remaining family) - which family this
    ENTIRE carousel (hook + all info slides) uses. Resolved once here (if
    not passed) so every slide in the post shares one family, then handed
    down to build_news_card / build_info_slide.

    `slide_texts` is a list of Hindi body-text chunks, one per info slide -
    each entry becomes its own slide. If empty, only the hook slide is built.

    grayscale: if True, info slides use a true black-and-white treatment
    instead of the category-colored duotone tint.

    theme: a dict with a "gradient" key (only the gradient is used here —
    see build_news_card's docstring on why the logo is NOT taken from
    this dict). Pass the SAME theme across every story in a batch so the
    whole post shares one consistent look. If not provided, a random
    theme is picked once for this carousel.

    breaking / breaking_label: if breaking=True, every slide gets a red
    breaking-news badge (Hindi "ब्रेकिंग" by default, override via
    breaking_label) next to its category pill. Only pass True for stories
    actually flagged as breaking news.

    highlight: an exact substring of `headline` to draw a highlighter-
    marker box behind on the hook slide. Pass None to skip. Silently
    skipped if not found verbatim in `headline`, or if grayscale=True.
    NOTE: Devanagari text can involve matra reordering during OpenType
    shaping (see this file's top docstring), so for a highlight phrase
    that starts or ends mid-conjunct the box's pixel position may be
    very slightly off from the true glyph edges - it's measured from
    logical string position, not shaped glyph position (Pillow doesn't
    expose the latter). Fine for the common case (phrase boundaries on
    clean word breaks), worth a visual spot-check if it looks off.

    Returns the list of output file paths, in post order.
    """
    theme = theme or random.choice(HEADLINE_THEMES)
    font_family = font_family or random.choice(FONT_FAMILY_CHOICES)
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
        breaking_label=breaking_label,
        grayscale=grayscale,
        font_family=font_family,
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
            breaking_label=breaking_label,
            font_family=font_family,
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

    sample_headline = "सरकार ने 2027 के लिए अक्षय ऊर्जा निवेश पर नई नीति की घोषणा की"
    build_news_card(
        placeholder_path,
        sample_headline,
        "रॉयटर्स",
        tag="BUSINESS",
        out_path="sample_output_hi.png",
    )
    print("Sample Hindi card generated.")

    sample_slides = [
        "इस नीति के तहत 2027 तक 50 गीगावाट नई सौर और पवन क्षमता जोड़ने का लक्ष्य रखा गया है, जिसे एक समर्पित इन्फ्रास्ट्रक्चर फंड और डेवलपर्स के लिए सरल भूमि-अधिग्रहण नियमों का समर्थन मिलेगा।",
        "उद्योग संगठनों ने इस घोषणा का स्वागत किया, लेकिन ग्रिड क्षमता और ट्रांसमिशन-लाइन मंजूरी की धीमी रफ्तार को लेकर चिंता भी जताई, जो आमतौर पर उत्पादन लक्ष्यों से पीछे रह जाती है।",
    ]
    build_carousel(
        placeholder_path,
        sample_headline,
        "रॉयटर्स",
        tag="BUSINESS",
        slide_texts=sample_slides,
        out_dir="output",
        base_filename="sample_carousel_hi",
    )
    print("Sample Hindi carousel generated (3 slides).")

    # Also demonstrate the auto-detect behavior with a Latin source name
    # (kept in Roman script, as is common for wire-service credit lines).
    build_news_card(
        placeholder_path,
        "आईपीएल 2027: फाइनल में पहुंचने वाली टीमों का ऐलान",
        "PTI",
        tag="SPORTS",
        breaking=True,
        out_path="sample_output_hi_breaking.png",
    )
    print("Sample breaking-news Hindi card generated (Latin source, Hindi headline+badge).")