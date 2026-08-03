# News Card Generator

Fetches a real news article, pulls its photo, and composites a styled
"news card" PNG (1080×1350, Instagram portrait) — headline + source on
a dark panel below the photo. No AI image generation involved, so the
output is deterministic and text is always crisp.

## How it works

1. `news_source.py` — pulls recent headlines from Google News RSS for a
   topic (or general top headlines), no API key needed.
2. `image_fetch.py` — resolves the Google News redirect link to the real
   article, scrapes its `og:image` tag, downloads the photo.
3. `card_generator.py` — crops the photo to fill the top of the canvas,
   adds a dark panel with a colored tag pill, wrapped/auto-sized
   headline, and source line at the bottom.
4. `main.py` — runs the full pipeline and tries the next article
   automatically if one has no usable image.

## Setup (Windows / PowerShell)

```powershell
cd C:\Users\HP\Downloads\news_card
pip install -r requirements.txt
```

## Usage

```powershell
python main.py "technology India"
python main.py "cricket"
python main.py                      # falls back to top headlines
```

Output lands in `output/card_<timestamp>.png`.

## Customizing the look

All styling constants are at the top of `card_generator.py`:

- `BG_COLOR`, `ACCENT_COLOR`, `TEXT_COLOR` — swap for your brand palette
- `FONT_HEADLINE`, `FONT_TAG`, `FONT_META` — point these at your own
  `.ttf` files (e.g. a bold condensed font) for a distinct brand look
- `CANVAS_W`, `CANVAS_H`, `IMAGE_H` — change aspect ratio / crop split
- `tag` param in `build_news_card()` — set per-category ("BREAKING",
  "TECH", "SPORTS", etc.) when you call it

## Notes / things to watch for

- **Rate limiting**: don't hammer Google News RSS in a tight loop —
  add a small delay if you're batch-generating many cards.
- **Image licensing**: the photo comes straight from the source
  article. For a public Instagram page you're responsible for making
  sure you have rights to repost it — many publishers watermark or
  don't want hotlinked republishing. Consider either (a) using this
  only for sources you have permission to use, or (b) swapping in a
  stock/AI-generated background for the photo when the topic doesn't
  strictly need the original image (this is where the earlier
  Pillow-gradient approach becomes useful again).
- `main.py` automatically skips to the next headline if an article has
  no `og:image`, since not every article carries one.
- This can't be tested end-to-end from Claude's sandbox (its network
  is locked to package registries only, not news sites) — it's tested
  here with a placeholder image feeding straight into
  `card_generator.py`, and the RSS/scraping code follows the same
  pattern used successfully elsewhere. Run it locally first with a
  couple of test queries and tell me what breaks if anything does.
