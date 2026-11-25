# ipa-feed

One AltStore source for every app I sideload. A GitHub Action rebuilds
`apps.json` every 6 hours from the developers' own sources and GitHub
releases, so LiveContainer shows updates without hunting for IPAs.

## Add it to LiveContainer

Sources → **+** → paste:

```
https://raw.githubusercontent.com/YOUR_GITHUB_USERNAME/ipa-feed/main/apps.json
```

## What it follows

Edit `apps.toml`. Two kinds of entry:

- `[[source]]`: the developer already publishes a source. Its apps are copied in.
- `[[github]]`: no source exists. The newest release with a matching `.ipa` becomes the entry.
  The IPA is downloaded once per version to read its bundle ID.

Commit the change and the Action runs automatically.

## Run it now

GitHub → **Actions** → **Update feed** → **Run workflow**.

## If an app goes missing

Open the latest Action run. Each failed app prints a `WARNING` line. If a source is down,
the last good copy is kept so the app doesn't vanish from LiveContainer.

## Test locally

```
python3 -m unittest discover -s tests
python3 scripts/build_feed.py
```
