# Private, free, always-on hosting (Oracle Cloud "Always Free" VM)

This runs the bot exactly as originally designed — `daily_scheduler.py`'s
resident loop, no code visible to anyone, no GitHub Actions minutes
involved at all. You can even skip GitHub entirely for this project if
you want; the steps below don't require it.

## 1. Create the free VM

1. Sign up at https://www.oracle.com/cloud/free/ (a card is required for
   identity verification only — nothing is charged unless you explicitly
   upgrade to a paid account later).
2. Console → Compute → Instances → **Create Instance**.
3. Shape: **VM.Standard.A1.Flex** (Ampere/ARM) — pick e.g. 2 OCPU / 12GB,
   well inside the Always Free allowance (up to 4 OCPU / 24GB total).
4. Image: **Ubuntu** (22.04 or later).
5. Add your SSH key (generate one locally with `ssh-keygen` if you don't
   have one) so you can log in.
6. If you get an "out of capacity" error for Ampere A1 in your chosen
   region, either try a different Availability Domain, try again later
   (capacity fluctuates), or fall back to Google Cloud's free `e2-micro`
   tier instead — the steps below are the same either way, just a
   different signup page.

## 2. Get your code onto the VM — without ever making it public

SSH in, then copy your project directly (scp), or clone it from a
**private** GitHub repo if you'd rather keep a backup/history there too
(a private repo is free regardless of visibility — the earlier tradeoff
only applied to running things *via GitHub Actions*, not to storing code
in a private repo. Cloning a private repo onto your own VM doesn't touch
Actions minutes at all).

```bash
# from your local machine
scp -r -i ~/.ssh/your_key /path/to/news_card ubuntu@<VM_PUBLIC_IP>:/opt/news_card
```

or, on the VM, if using a private repo:

```bash
git clone git@github.com:yourname/news_card.git /opt/news_card
```

## 3. Install dependencies on the VM

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip
cd /opt/news_card
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 4. Add your real `.env`

Create `/opt/news_card/.env` on the VM with your actual values (same
keys as before: SUPABASE_URL, SUPABASE_SERVICE_KEY,
SUPABASE_STORAGE_BUCKET, IG_USER_ID, IG_ACCESS_TOKEN, GEMINI_API_KEY,
META_APP_ID, META_APP_SECRET — plus, for the Hindi sister page,
IG_USER_ID_HI and IG_ACCESS_TOKEN_HI. Set POST_HINDI_PAGE=false to
pause Hindi posting without touching the English pipeline). This file
never leaves the VM.

Then run the one-time migration so Supabase has your current token's
tracked expiry:

```bash
.venv/bin/python seed_supabase_state.py
```

## 5. Run it as a systemd service (auto-restarts on crash or reboot)

```bash
sudo cp deploy/daily_scheduler.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now daily_scheduler
```

Check it's alive and watch logs:

```bash
sudo systemctl status daily_scheduler
sudo journalctl -u daily_scheduler -f
```

That's it — the VM stays on 24/7 for free, indefinitely, your code is
never visible to anyone, and there's no GitHub Actions minute budget to
think about at all. If you later decide you don't mind a public repo,
you can always switch back to the GitHub Actions approach — nothing
here is exclusive to one path.
