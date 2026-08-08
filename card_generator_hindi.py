"""
card_generator_hindi.py
Hindi (Devanagari) version of card_generator.py — same "news card" layout
and logic (1080x1350 Instagram-portrait card, hook slide + info-slide
carousel), but the headline/body/tag/source copy is Hindi and only two
font families are used, ROTATING one-by-one across posts (never mixed
within a single card — every text element on one card uses the same
family, so the card reads as one consistent typographic choice):

  - Kalam                  -> handwritten-style Devanagari + Latin face.
    Weights available: Light, Regular, Bold.
  - Eczar                  -> serif slab Devanagari + Latin face.
    Weights available: Regular, Medium, SemiBold, Bold, ExtraBold.

Both families have full native Devanagari AND Latin glyph coverage, so
(unlike the old Inknut Antiqua / League Spartan setup) there's no
per-string script detection needed anymore — whichever family is active
for this card renders every string on it, Hindi or Latin alike.

Which family is "active" for a given card is passed in via the
`font_family` param ("kalam" or "eczar") on build_news_card /
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
import os as _os
import random
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps, features as _pil_features

CANVAS_W, CANVAS_H = 1080, 1350       # Instagram portrait
IMAGE_H = 900                          # photo area height (hook slide, 2:1 photo:panel split)
LOGO_SIZE = 140                        # brand logo badge, bottom-right corner
PANEL_H = CANVAS_H - IMAGE_H           # text panel height

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
HEADLINE_THEMES = [
    {
        "name": "silver",
        "gradient": ["#858489", "#e7e4ef", "#858489", "#b9b9b9", "#858489"],
        "logo": _os.path.join(_ASSETS_DIR, "logo_black_white.png"),
    },
    {
        "name": "bronze_gold",
        "gradient": ["#785c3a", "#e2c29a", "#785c3a", "#ac8e68", "#785c3a"],
        "logo": _os.path.join(_ASSETS_DIR, "logo_golden.png"),
    },
    {
        "name": "warm_taupe",
        "gradient": ["#8b806f", "#e8decc", "#8b806f", "#b3ae9a", "#8d8c88"],
        "logo": _os.path.join(_ASSETS_DIR, "logo_silver.png"),
    },
]

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


# --- fonts ---------------------------------------------------------------
# Only the two attached families are used anywhere in this file, and a
# whole card uses exactly ONE of them (see `font_family` param below) —
# rotation between the two happens across posts, never within one card.
_FONT_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "fonts_hindi")

# Per-family, per-role font file. Both families cover Devanagari + Latin
# natively, so one file per role is enough — no separate Deva/Latin
# routing needed the way the old Inknut/League setup required.
FONT_FAMILIES = {
    "kalam": {
        # Light is used for every role - Kalam's Bold cut is intentionally
        # never referenced anywhere in this file.
        "headline": _os.path.join(_FONT_DIR, "Kalam-Light.ttf"),
        "body": _os.path.join(_FONT_DIR, "Kalam-Light.ttf"),
        "tag": _os.path.join(_FONT_DIR, "Kalam-Light.ttf"),
        "meta": _os.path.join(_FONT_DIR, "Kalam-Light.ttf"),
    },
    "eczar": {
        # Eczar has a full weight range and declares both dev2 + legacy
        # deva script tables, so it shapes correctly (verified) unlike
        # Tiro Devanagari Hindi, which was dropped for this reason.
        # Regular (400) is Eczar's lightest cut - its variable-font axis
        # only goes from 400-800, there's no Light/Thin instance below
        # Regular - so Regular is used for every role here, never Bold+.
        "headline": _os.path.join(_FONT_DIR, "Eczar-Regular.ttf"),
        "body": _os.path.join(_FONT_DIR, "Eczar-Regular.ttf"),
        "tag": _os.path.join(_FONT_DIR, "Eczar-Regular.ttf"),
        "meta": _os.path.join(_FONT_DIR, "Eczar-Regular.ttf"),
    },
}

# Order used for standalone/random fallback (when no font_family is
# passed in) — hourly_run.py drives the real strict one-by-one rotation
# externally and always passes font_family explicitly.
FONT_FAMILY_CHOICES = ["kalam", "eczar"]
DEFAULT_FONT_FAMILY = "kalam"


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


def _wrap_by_width(draw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list:
    """Greedily wraps text into lines that actually fit max_width in
    pixels for the given font, instead of a fixed character count (which
    under- or over-fills the line depending on font/size). Word-splitting
    on whitespace works the same way for Hindi as for English since
    Devanagari text is space-separated between words."""
    words = text.split()
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
                   max_size: int, min_size: int, line_spacing_extra: int, step: int = 2):
    """
    Picks the LARGEST font size (within [min_size, max_size]) whose
    pixel-wrapped text fits inside max_width x max_height. This makes
    short copy render bigger (filling the box) and long copy render
    smaller (fitting the box) instead of one fixed size that leaves
    empty space for short text and gets truncated for long text.

    Returns (font, wrapped_lines, line_height). If even min_size doesn't
    fit, the text is truncated with an ellipsis as a last resort.
    """
    for size in range(max_size, min_size - 1, -step):
        font = _load_font(font_path, size)
        lines = _wrap_by_width(draw, text, font, max_width)
        ascent, descent = font.getmetrics()
        line_h = ascent + descent + line_spacing_extra
        if line_h * len(lines) <= max_height:
            return font, lines, line_h

    font = _load_font(font_path, min_size)
    lines = _wrap_by_width(draw, text, font, max_width)
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
    text_block_h = line_height * len(lines)

    # Full-height gradient - color only varies by row, so any horizontal
    # slice of it at a given y is valid, which is what makes per-line
    # centering with a continuous vertical shimmer possible below.
    full_gradient = _make_linear_gradient(max_w + 4, text_block_h + 4, hex_stops)

    for i, (line, line_w) in enumerate(zip(lines, line_widths)):
        mask = Image.new("L", (line_w + 4, line_height + 4), 0)
        ImageDraw.Draw(mask).text((0, 0), line, font=font, fill=255)
        gradient_slice = full_gradient.crop((0, i * line_height, line_w + 4, i * line_height + line_height + 4))
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
):
    """Same layout as the English generator's build_news_card, but all
    copy is expected to be Hindi. `tag` may be a canonical English key
    (translated automatically via CATEGORY_LABELS_HI) or an already-
    localized string.

    Note on `theme`: only its "gradient" is used here (for the headline
    color) — its "logo" is ignored. The bottom-right badge always comes
    from HINDI_LOGO_PATH, since `theme` in the real pipeline is actually
    the English module's theme object (see the file docstring).

    font_family: "kalam" or "eczar" - which of the two rotating font
    families this whole card uses (every text element on the card, not
    just the headline). If omitted, a random family is picked - callers
    that want strict one-by-one rotation across posts should always
    pass this explicitly (see hourly_run.py's _next_font_family)."""
    theme = theme or random.choice(HEADLINE_THEMES)
    font_family = font_family or random.choice(FONT_FAMILY_CHOICES)
    headline_font_path = _font_path(font_family, "headline")
    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), BG_COLOR)

    # --- top: photo (cropped to fill) or a generated gradient background ---
    is_generated_bg = photo_path is None
    if photo_path:
        photo = Image.open(photo_path).convert("RGB")
        photo = crop_to_fill(photo, CANVAS_W, IMAGE_H)
    else:
        photo = generate_gradient_background(CANVAS_W, IMAGE_H, tag=tag)

    # For serious/sensitive stories (deaths, sexual assault, murder, etc.)
    # the whole background - real photo or generated gradient alike -
    # is rendered as true black-and-white instead of the day's category
    # color, so these stories read as visually distinct and sober rather
    # than sharing the same bright/colorful treatment as routine news.
    if grayscale:
        photo = ImageOps.grayscale(photo).convert("RGB")

    # slight bottom gradient fade so the top blends into the panel
    fade = Image.new("L", (CANVAS_W, IMAGE_H), 0)
    fade_draw = ImageDraw.Draw(fade)
    fade_height = 160
    for y in range(fade_height):
        alpha = int(255 * (y / fade_height))
        fade_draw.line([(0, IMAGE_H - fade_height + y), (CANVAS_W, IMAGE_H - fade_height + y)], fill=alpha)
    black_layer = Image.new("RGB", (CANVAS_W, IMAGE_H), BG_COLOR)
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
            IMAGE_H - 50 - label_h - label_pad * 2,
            CANVAS_W - pad_x,
            IMAGE_H - 50,
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
    # to the left of the category pill, same row, fixed red/white so it
    # never blends into the day's theme color ---
    tag_start_x = pad_x
    if breaking:
        breaking_font = _load_font(_font_path(font_family, "tag"), 30)
        breaking_box = _draw_breaking_badge(draw, pad_x, pad_y, breaking_label, breaking_font)
        tag_start_x = breaking_box[2] + 12  # small gap before the category pill

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
    panel_top = IMAGE_H + 45
    meta_y = CANVAS_H - 70
    logo_reserved_gap = 45
    logo_top = (CANVAS_H - pad_y) - LOGO_SIZE  # logo sits low, near the true bottom edge
    logo_left = CANVAS_W - pad_x - LOGO_SIZE
    panel_bottom_full = CANVAS_H - 90  # top of the source-line divider
    available_h = panel_bottom_full - panel_top

    # min_size/line_spacing bumped up slightly vs. the English generator:
    # Devanagari matras/conjuncts need a bit more vertical breathing room
    # and stop being legible sooner than Latin does as size shrinks.
    headline_font, wrapped, line_height = _autofit_text(
        draw, headline, headline_font_path, max_text_width, available_h,
        max_size=150, min_size=60, line_spacing_extra=16,
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
            # Only re-fit into the smaller box if the last line would
            # actually reach into the logo's horizontal corner too -
            # most centered short lines never get that far right.
            if last_line_x_end > logo_left - logo_reserved_gap:
                available_h = (logo_top - logo_reserved_gap) - panel_top
                headline_font, wrapped, line_height = _autofit_text(
                    draw, headline, headline_font_path, max_text_width, available_h,
                    max_size=150, min_size=60, line_spacing_extra=16,
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
    gradient_stops = ["#ffffff", "#e0e0e0", "#ffffff", "#f2f2f2", "#ffffff"] if grayscale else theme["gradient"]
    _draw_gradient_text(canvas, (pad_x, text_y), wrapped, headline_font, line_height, gradient_stops,
                         block_width=max_text_width, center=True)

    # --- source / meta line at the bottom of the panel, and the brand logo ---
    meta_font = _load_font(_font_path(font_family, "meta"), 26)
    logo_bottom_y = CANVAS_H - pad_y
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

    font_family: "kalam" or "eczar" - same rotation param as
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

    # --- BREAKING badge, same rule as the hook slide: only for stories
    # flagged as breaking, sits left of the category pill ---
    tag_start_x = pad_x
    if breaking:
        breaking_font = _load_font(_font_path(font_family, "tag"), 26)
        breaking_box = _draw_breaking_badge(draw, pad_x, pad_y, breaking_label, breaking_font)
        tag_start_x = breaking_box[2] + 12

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
        max_size=58, min_size=34, line_spacing_extra=24,
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
) -> list:
    """
    Builds a full Hindi carousel: slide 1 is the eye-catching hook (photo +
    short Hindi headline, via build_news_card), slides 2..N are
    informational, using real (translated) article text and a duotone-
    tinted variant of the same photo (or a per-slide generated background
    if there's no photo).

    font_family: "kalam" or "eczar" - which rotating font family this
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
