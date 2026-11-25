#!/usr/bin/env python3
"""Build one AltStore-format source (apps.json) from apps.toml.

[[source]] entries are copied from existing AltStore sources.
[[github]] entries are built from each repo's newest release with an .ipa.

The previous apps.json is reused as a cache: an IPA is only downloaded
(to read its bundle identifier) when its version is new.

Usage: build_feed.py [--config apps.toml] [--out apps.json]
Env:   GITHUB_TOKEN (optional, raises the GitHub API rate limit)
"""
from __future__ import annotations

import argparse
import io
import json
import os
import plistlib
import re
import sys
import tomllib
import urllib.parse
import urllib.request
import zipfile

UA = "ipa-feed/1.0"


# ---------------------------------------------------------------- network

def http_get(url: str, *, api: bool = False, timeout: int = 60) -> bytes:
    headers = {"User-Agent": UA}
    if api:
        headers["Accept"] = "application/vnd.github+json"
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def get_json(url: str, *, api: bool = False):
    return json.loads(http_get(url, api=api))


# ---------------------------------------------------------------- helpers

def app_version(app: dict) -> str | None:
    versions = app.get("versions") or []
    if versions:
        return versions[0].get("version")
    return app.get("version")


def bundle_id_from_ipa(data: bytes) -> tuple[str | None, str | None, str | None]:
    """Return (bundleIdentifier, CFBundleShortVersionString, min iOS) from an IPA."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", name):
                info = plistlib.loads(z.read(name))
                return (
                    info.get("CFBundleIdentifier"),
                    info.get("CFBundleShortVersionString"),
                    info.get("MinimumOSVersion"),
                )
    return None, None, None


def pick_release(releases: list[dict], entry: dict) -> tuple[dict, dict] | None:
    pattern = re.compile(entry.get("asset", r"\.ipa$"), re.IGNORECASE)
    prefix = entry.get("tag_prefix", "")
    allow_pre = entry.get("prerelease", False)
    for rel in releases:  # API returns newest first
        if rel.get("draft"):
            continue
        if rel.get("prerelease") and not allow_pre:
            continue
        if prefix and not rel.get("tag_name", "").startswith(prefix):
            continue
        for asset in rel.get("assets", []):
            if pattern.search(asset["name"]):
                return rel, asset
    return None


# ---------------------------------------------------------------- builders

def apps_from_source(entry: dict) -> list[dict]:
    data = get_json(entry["url"])
    apps = data.get("apps", [])
    only = [o.lower() for o in entry.get("only", [])]
    if only:
        apps = [a for a in apps if any(o in a.get("name", "").lower() for o in only)]
    return apps


def app_from_github(entry: dict, cache: dict[str, dict]) -> dict:
    repo = entry["repo"]
    releases = get_json(f"https://api.github.com/repos/{repo}/releases?per_page=30", api=True)
    picked = pick_release(releases, entry)
    if not picked:
        raise RuntimeError(f"no release in {repo} has an asset matching {entry.get('asset')!r}")
    rel, asset = picked

    tag = rel["tag_name"]
    version = re.sub(r"^(ios-|v)", "", tag, flags=re.IGNORECASE)
    cached = cache.get(repo)

    if cached and cached.get("_tag") == tag and cached.get("bundleIdentifier"):
        bundle_id = cached["bundleIdentifier"]
        min_os = cached["versions"][0].get("minOSVersion")
    else:
        bundle_id, plist_version, min_os = bundle_id_from_ipa(
            http_get(asset["browser_download_url"], timeout=600)
        )
        if not bundle_id:
            raise RuntimeError(f"{asset['name']} has no Payload/*.app/Info.plist")
        version = plist_version or version

    date = rel.get("published_at") or rel.get("created_at")
    ver = {
        "version": version,
        "date": date,
        "localizedDescription": (rel.get("body") or "")[:4000],
        "downloadURL": asset["browser_download_url"],
        "size": asset["size"],
    }
    if min_os:
        ver["minOSVersion"] = min_os

    owner = repo.split("/")[0]
    return {
        "name": entry["name"],
        "bundleIdentifier": bundle_id,
        "developerName": owner,
        "subtitle": entry.get("subtitle", f"From github.com/{repo}"),
        "localizedDescription": entry.get("description", f"Built automatically from the latest release of github.com/{repo}."),
        "iconURL": entry.get("icon", f"https://github.com/{owner}.png"),
        "versions": [ver],
        # legacy top-level fields, still read by some installers
        "version": ver["version"],
        "versionDate": date,
        "downloadURL": ver["downloadURL"],
        "size": ver["size"],
        "_repo": repo,
        "_tag": tag,
    }


def head(url: str) -> dict[str, str]:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return {k.lower(): v for k, v in resp.headers.items()}


def resolve_url(entry: dict) -> str:
    """Find the newest download link on `page` matching `pattern`, else use `url`."""
    if entry.get("page") and entry.get("pattern"):
        try:
            html = http_get(entry["page"]).decode("utf-8", "replace")
            found = re.findall(entry["pattern"], html)
            if found:
                return found[0]
        except Exception:
            pass
    if not entry.get("url"):
        raise RuntimeError("no download link found and no fallback `url` set")
    return entry["url"]


def app_from_url(entry: dict, cache: dict[str, dict]) -> dict:
    """An IPA at a plain download link. Re-read only when the file changes."""
    url = resolve_url(entry)
    h: dict[str, str] = {}
    try:
        h = head(url)
        fingerprint = h.get("etag") or h.get("last-modified") or h.get("content-length") or ""
        size = int(h.get("content-length") or 0)
    except Exception:
        fingerprint, size = "", 0

    key = entry["name"]
    cached = cache.get(key)
    if cached and fingerprint and cached.get("_fingerprint") == fingerprint and cached.get("downloadURL") == url:
        return cached

    data = http_get(url, timeout=900)
    bundle_id, version, min_os = bundle_id_from_ipa(data)
    if not bundle_id:
        raise RuntimeError(f"{url} has no Payload/*.app/Info.plist")
    size = size or len(data)
    date = h.get("last-modified") if fingerprint else None
    ver = {"version": version or "0", "downloadURL": url, "size": size}
    if date:
        ver["date"] = date
    if min_os:
        ver["minOSVersion"] = min_os
    return {
        "name": entry["name"],
        "bundleIdentifier": bundle_id,
        "developerName": entry.get("developer", urllib.parse.urlparse(url).hostname),
        "subtitle": entry.get("subtitle", ""),
        "localizedDescription": entry.get("description", f"From {url}"),
        "iconURL": entry.get("icon", ""),
        "versions": [ver],
        "version": ver["version"],
        "downloadURL": url,
        "size": size,
        "_url_key": key,
        "_fingerprint": fingerprint,
    }


# ---------------------------------------------------------------- main

def build(config: dict, previous: dict | None) -> tuple[dict, list[str]]:
    errors: list[str] = []
    cache = {a["_repo"]: a for a in (previous or {}).get("apps", []) if a.get("_repo")}
    prev_by_origin: dict[str, list[dict]] = {}
    for a in (previous or {}).get("apps", []):
        prev_by_origin.setdefault(a.get("_origin", ""), []).append(a)

    apps: list[dict] = []

    for entry in config.get("source", []):
        try:
            got = apps_from_source(entry)
            if not got:
                raise RuntimeError("source returned no matching apps")
            for a in got:
                a["_origin"] = entry["url"]
            apps.extend(got)
        except Exception as exc:  # keep last good copy so one outage doesn't drop an app
            errors.append(f"[source] {entry['name']}: {exc}")
            apps.extend(prev_by_origin.get(entry["url"], []))

    for entry in config.get("github", []):
        try:
            a = app_from_github(entry, cache)
            a["_origin"] = entry["repo"]
            apps.append(a)
        except Exception as exc:
            errors.append(f"[github] {entry['name']}: {exc}")
            if entry["repo"] in cache:
                apps.append(cache[entry["repo"]])

    url_cache = {a["_url_key"]: a for a in (previous or {}).get("apps", []) if a.get("_url_key")}
    for entry in config.get("url", []):
        try:
            a = app_from_url(entry, url_cache)
            a["_origin"] = entry["name"]
            apps.append(a)
        except Exception as exc:
            errors.append(f"[url] {entry['name']}: {exc}")
            if entry["name"] in url_cache:
                apps.append(url_cache[entry["name"]])

    # de-duplicate by bundle identifier, first one wins
    seen: set[str] = set()
    unique = []
    for a in apps:
        key = a.get("bundleIdentifier") or a.get("name")
        if key in seen:
            continue
        seen.add(key)
        unique.append(a)

    feed = config["feed"]
    source = {
        "name": feed["name"],
        "identifier": feed["identifier"],
        "subtitle": feed.get("subtitle", ""),
        "description": "Generated by ipa-feed. Edit apps.toml to change what it follows.",
        "apps": unique,
        "news": [],
    }
    return source, errors


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="apps.toml")
    p.add_argument("--out", default="apps.json")
    args = p.parse_args()

    with open(args.config, "rb") as f:
        config = tomllib.load(f)
    previous = None
    if os.path.exists(args.out):
        with open(args.out) as f:
            previous = json.load(f)

    source, errors = build(config, previous)

    with open(args.out, "w") as f:
        json.dump(source, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"wrote {args.out}: {len(source['apps'])} apps")
    for a in source["apps"]:
        print(f"  {a.get('name')}  {app_version(a)}")
    for e in errors:
        print(f"WARNING {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
