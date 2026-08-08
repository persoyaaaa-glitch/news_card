# Checks EVERY codepoint in the Unicode Devanagari block (U+0900-U+097F)
# against each font's cmap, so we don't have to guess character-by-character.
from fontTools.ttLib import TTFont
import unicodedata

for path in ["fonts_hindi/Kalam-Light.ttf", "fonts_hindi/Eczar-Regular.ttf"]:
    cmap = TTFont(path).getBestCmap()
    missing = []
    for cp in range(0x0900, 0x0980):
        try:
            name = unicodedata.name(chr(cp))
        except ValueError:
            continue  # unassigned codepoint - skip
        if cp not in cmap:
            missing.append((hex(cp), chr(cp), name))
    print(f"\n=== {path} ===")
    if not missing:
        print("Full Devanagari block coverage - nothing missing.")
    else:
        for hexcode, ch, name in missing:
            print(f"  {hexcode}  {ch!r}  {name}")