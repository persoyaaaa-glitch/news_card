"""
test_post.py
One-off test run of the full pipeline: surfaces news, builds the card
images (with the BREAKING badge if applicable), generates the caption +
hashtags + song suggestion, and publishes ONE post to Instagram.

By default this runs in PREVIEW mode - it builds everything (images,
caption, hashtags, song) and prints/saves it locally, but does NOT
upload anything or touch Instagram, and does NOT mark the story as
posted. That way you can re-run it as many times as you want while
testing without burning your daily post budget or needing to worry
about accidentally spamming your real account.

When you're ready for an actual live test post, pass --live.

For a proper before-you-go-live trial across many stories at once (the
same pipeline, run repeatedly), pass --batch: builds N distinct stories
(default 10, ~2 images each = ~20 images total) and saves every
story's slides + caption to its own folder under output/test_batch_*/,
plus a summary you can scan for real-image vs generated-background
ratio, whether description slides are showing up, and whether a song
suggestion made it into the caption. Never posts or marks anything as
posted, no matter what.

Usage:
    python test_post.py                # preview only, nothing posted
    python test_post.py --live         # posts once, for real, to Instagram
    python test_post.py --batch        # trial: 10 stories, preview only, no posting
    python test_post.py --batch --batch-count 5   # trial: 5 stories

    python test_post.py --multi                    # preview: 10 stories, as if posting for real (no upload)
    python test_post.py --multi --live              # POSTS 10 real, separate Instagram posts (20 images)
    python test_post.py --multi --multi-count 5 --live   # posts 5 real stories instead of 10

    python test_post.py --hindi                    # preview: builds + translates one story, nothing posted
    python test_post.py --hindi --live              # POSTS 1 real post to the HINDI account ONLY
                                                     # (English account is never touched, and the story is
                                                     # never marked as posted, so it doesn't affect the
                                                     # normal automated schedule at all)
"""
import argparse
import json
import os
import shutil
import sys

import hourly_run

OUTPUT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
PREVIEW_DIR = os.path.join(OUTPUT_ROOT, "test_preview")


def _print_result(result: dict, live: bool):
    if not result:
        print("\nNo postable story found this run (all candidates were duplicates, "
              "or nothing had a usable image/text). Try again in a bit.")
        return

    print("\n" + "=" * 60)
    print(f"TEST {'POST (LIVE)' if live else 'PREVIEW (nothing published)'}")
    print("=" * 60)
    print(f"Headline : {result['title']}")
    print(f"Source   : {result['source']}")
    print(f"Category : {result['tag']}")
    print(f"Breaking : {result['is_breaking']}")
    print(f"Link     : {result['link']}")
    print(f"Slides   : {len(result['slide_paths'])} -> {', '.join(result['slide_paths'])}")
    print("-" * 60)
    print("CAPTION (includes hashtags + suggested song mention):\n")
    print(result["caption"])
    print("-" * 60)

    if live:
        print(f"Instagram media ID: {result.get('media_id')}")
        print("This story is now marked as posted and will never be repeated.")
    else:
        os.makedirs(PREVIEW_DIR, exist_ok=True)
        meta_path = os.path.join(PREVIEW_DIR, "last_preview.json")
        with open(meta_path, "w") as f:
            json.dump(
                {k: v for k, v in result.items() if k != "media_id"},
                f, indent=2,
            )
        print(f"Preview metadata saved to {meta_path}")
        print("Nothing was uploaded or posted, and this story was NOT marked as "
              "posted - it's still available for a real run (--live) or the "
              "scheduled pipeline.")
    print("=" * 60)


def _run_batch_trial(story_count: int, max_attempts: int):
    """
    Runs the N-story QA trial, saves each story's slides/caption/meta
    into its own folder, and prints a scannable summary so you can spot
    whether real images, description slides, and song suggestions are
    actually showing up before trusting a --live run. Never posts,
    uploads, or marks anything as posted.
    """
    from datetime import datetime as _dt
    batch_dir = os.path.join(OUTPUT_ROOT, f"test_batch_{_dt.now().strftime('%Y%m%d_%H%M%S')}")
    os.makedirs(batch_dir, exist_ok=True)

    results = hourly_run.run_batch(story_count=story_count, max_attempts=max_attempts, out_dir=batch_dir)

    if not results:
        print("\nNo stories could be built for this trial (all duplicates, or nothing "
              "had usable image/text). Try again in a bit, or raise --max-attempts.")
        return

    for i, result in enumerate(results, 1):
        story_dir = os.path.join(batch_dir, f"story_{i:02d}")
        os.makedirs(story_dir, exist_ok=True)

        moved_paths = []
        for slide_path in result["slide_paths"]:
            dest = os.path.join(story_dir, os.path.basename(slide_path))
            if os.path.abspath(slide_path) != os.path.abspath(dest):
                shutil.move(slide_path, dest)
            moved_paths.append(dest)
        result["slide_paths"] = moved_paths

        with open(os.path.join(story_dir, "caption.txt"), "w", encoding="utf-8") as f:
            f.write(result["caption"])
        with open(os.path.join(story_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)

    with open(os.path.join(batch_dir, "index.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    total_images = sum(len(r["slide_paths"]) for r in results)
    real_image_count = sum(1 for r in results if r["used_real_image"])
    description_slide_count = sum(1 for r in results if r["has_description_slide"])
    song_count = sum(1 for r in results if r["has_song"])

    print("\n" + "=" * 60)
    print(f"BATCH TRIAL - {len(results)} stories, {total_images} images (preview only, nothing posted)")
    print("=" * 60)
    for i, r in enumerate(results, 1):
        tag_label = "BREAKING" if r["is_breaking"] else r["tag"]
        print(f"{i:2d}. [{tag_label:8s}] {r['title'][:55]}")
        print(f"     image={'real' if r['used_real_image'] else 'generated'}  "
              f"description_slide={'yes' if r['has_description_slide'] else 'no'}  "
              f"song={'yes' if r['has_song'] else 'no'}  "
              f"slides={len(r['slide_paths'])}")
    print("-" * 60)
    print(f"Real photo used   : {real_image_count}/{len(results)}")
    print(f"Description slide : {description_slide_count}/{len(results)}")
    print(f"Song suggestion   : {song_count}/{len(results)}")
    print(f"\nAll files saved under: {batch_dir}")
    print("=" * 60)


def _print_result_hindi(result: dict, live: bool):
    if not result:
        print("\nNo postable story found this run (all candidates were duplicates, "
              "or nothing had a usable image/text, or every translation attempt failed). "
              "Try again in a bit.")
        return

    print("\n" + "=" * 60)
    print(f"HINDI TEST {'POST (LIVE, HINDI ACCOUNT ONLY)' if live else 'PREVIEW (nothing published)'}")
    print("=" * 60)
    print(f"Headline (EN) : {result['title']}")
    print(f"Headline (HI) : {result['headline_hi']}")
    print(f"Source        : {result['source']}")
    print(f"Link          : {result['link']}")
    print(f"Slides        : {len(result['slide_paths_hi'])} -> {', '.join(result['slide_paths_hi'])}")
    print("-" * 60)
    print("CAPTION (Hindi, includes hashtags):\n")
    print(result["caption_hi"])
    print("-" * 60)

    if live:
        print(f"Instagram media ID (hi): {result.get('media_id_hi')}")
        print("Posted to the HINDI account only - the English account was never touched, "
              "and this story was NOT marked as posted, so it's still fully available "
              "for the real English/combined pipeline.")
    else:
        os.makedirs(PREVIEW_DIR, exist_ok=True)
        meta_path = os.path.join(PREVIEW_DIR, "last_preview_hi.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in result.items() if k != "media_id_hi"}, f, indent=2, ensure_ascii=False)
        print(f"Preview metadata saved to {meta_path}")
        print("Nothing was uploaded or posted, and this story was NOT marked as posted.")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Run the pipeline once (or as a multi-story trial), for testing.")
    parser.add_argument("--live", action="store_true",
                         help="Actually publish to Instagram (default: preview only, nothing posted). "
                              "Ignored if --batch is passed - batch trials are always preview-only.")
    parser.add_argument("--hindi", action="store_true",
                         help="Test-post ONE story to the Hindi account only (English is never touched, "
                              "and the story is never marked as posted). Preview-only unless --live is "
                              "also passed. Takes priority over --batch/--multi if combined.")
    parser.add_argument("--max-attempts", type=int, default=30,
                         help="How many candidate stories to fetch/screen before giving up (default: 30).")
    parser.add_argument("--batch", action="store_true",
                         help="Run a multi-story QA trial instead of a single post (preview only, never posts).")
    parser.add_argument("--batch-count", type=int, default=10,
                         help="Number of distinct stories to build in --batch mode (default: 10).")
    parser.add_argument("--multi", action="store_true",
                         help="Run the real multi-story posting flow (hourly_run.run_multiple): "
                              "story_count separate posts, priority order, song on every story. "
                              "Preview-only (dry_run) unless --live is also passed.")
    parser.add_argument("--multi-count", type=int, default=10,
                         help="Number of stories to post in --multi mode (default: 10).")
    args = parser.parse_args()

    if args.hindi:
        if args.live:
            confirm = input(
                "This will publish ONE real post to your HINDI Instagram account ONLY "
                "(English account will not be touched). Type 'yes' to continue: "
            )
            if confirm.strip().lower() != "yes":
                print("Cancelled - nothing was posted.")
                sys.exit(0)
        result = hourly_run.run_hindi_test(max_attempts=args.max_attempts, dry_run=not args.live)
        _print_result_hindi(result, live=args.live)
        return

    if args.multi:
        if args.live:
            confirm = input(
                f"This will publish {args.multi_count} REAL, separate posts to your connected "
                f"Instagram account (priority order, one post per story). Type 'yes' to continue: "
            )
            if confirm.strip().lower() != "yes":
                print("Cancelled - nothing was posted.")
                sys.exit(0)
        results = hourly_run.run_multiple(
            story_count=args.multi_count,
            max_attempts=max(args.max_attempts, args.multi_count * 8),
            apply_jitter=False,
            dry_run=not args.live,
        )
        print("\n" + "=" * 60)
        print(f"MULTI {'POST (LIVE)' if args.live else 'PREVIEW (nothing published)'} - "
              f"{len(results)}/{args.multi_count} stories")
        print("=" * 60)
        for i, r in enumerate(results, 1):
            tag_label = "BREAKING" if r["is_breaking"] else r["tag"]
            print(f"{i:2d}. priority=#{r['priority_rank']:<3} [{tag_label:9s}] {r['title'][:55]}")
            if args.live:
                print(f"     media_id={r.get('media_id')}")
        return

    if args.batch:
        # Fan out further by default since some candidates get filtered
        # out (duplicates, thin articles) before reaching batch-count.
        _run_batch_trial(story_count=args.batch_count, max_attempts=max(args.max_attempts, args.batch_count * 6))
        return

    if args.live:
        confirm = input(
            "This will publish ONE real post to your connected Instagram account. "
            "Type 'yes' to continue: "
        )
        if confirm.strip().lower() != "yes":
            print("Cancelled - nothing was posted.")
            sys.exit(0)

    result = hourly_run.run(max_attempts=args.max_attempts, apply_jitter=False, dry_run=not args.live)
    _print_result(result, live=args.live)


if __name__ == "__main__":
    main()
