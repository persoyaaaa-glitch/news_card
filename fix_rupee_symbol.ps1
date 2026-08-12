param(
    [string]$FilePath = ".\card_generator.py"
)

$content = Get-Content -Raw -LiteralPath $FilePath

$oldHexToRgb = @'
def _hex_to_rgb(hex_color: str) -> tuple:
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
'@

$newHexToRgb = @'
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
'@

$oldHookBlock = @'
    theme = theme or random.choice(HEADLINE_THEMES)
    # Hook headline font: defaults to the same font as the description slide
    # (FONT_BODY / Playfair) unless the caller pins a specific one, or passes
    # "random" to get the old per-card random pick from FONT_HEADLINE_CHOICES.
    if headline_font == "random":
'@

$newHookBlock = @'
    theme = theme or random.choice(HEADLINE_THEMES)
    headline = _sanitize_currency_symbols(headline)
    source = _sanitize_currency_symbols(source)
    tag = _sanitize_currency_symbols(tag)
    # Hook headline font: defaults to the same font as the description slide
    # (FONT_BODY / Playfair) unless the caller pins a specific one, or passes
    # "random" to get the old per-card random pick from FONT_HEADLINE_CHOICES.
    if headline_font == "random":
'@

$oldInfoBlock = @'
    tint_override: optional (dark_hex, light_hex) pair overriding the
    category's default duotone color, e.g. ("#000000", "#ffffff") for a
    true black-and-white treatment instead of a category-colored tint.
    """
    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), BG_COLOR)
'@

$newInfoBlock = @'
    tint_override: optional (dark_hex, light_hex) pair overriding the
    category's default duotone color, e.g. ("#000000", "#ffffff") for a
    true black-and-white treatment instead of a category-colored tint.
    """
    body_text = _sanitize_currency_symbols(body_text)
    tag = _sanitize_currency_symbols(tag)
    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), BG_COLOR)
'@

if ($content -notmatch [regex]::Escape($oldHexToRgb)) { throw "Anchor 1 (_hex_to_rgb) not found - file may already be patched or has diverged." }
if ($content -notmatch [regex]::Escape($oldHookBlock)) { throw "Anchor 2 (build_news_card theme block) not found." }
if ($content -notmatch [regex]::Escape($oldInfoBlock)) { throw "Anchor 3 (build_info_slide docstring block) not found." }

$content = $content.Replace($oldHexToRgb, $newHexToRgb)
$content = $content.Replace($oldHookBlock, $newHookBlock)
$content = $content.Replace($oldInfoBlock, $newInfoBlock)

Set-Content -LiteralPath $FilePath -Value $content -NoNewline -Encoding UTF8

python -c "import ast; ast.parse(open(r'$FilePath', encoding='utf-8').read())"
if ($LASTEXITCODE -ne 0) { throw "Patched file failed to parse as valid Python - review $FilePath." }

Write-Host "Patched: $FilePath"
