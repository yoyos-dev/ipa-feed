"""Offline tests: fakes the network so the generator's logic is checked
without reaching GitHub. Run: python3 -I -m unittest discover tests"""
import io
import json
import plistlib
import sys
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import build_feed  # noqa: E402


def fake_ipa(bundle_id: str, version: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "Payload/App.app/Info.plist",
            plistlib.dumps({"CFBundleIdentifier": bundle_id,
                            "CFBundleShortVersionString": version,
                            "MinimumOSVersion": "17.4"}),
        )
    return buf.getvalue()


RELEASES = {
    "willfaust/Madeira": [
        {"tag_name": "v0.3.0", "draft": False, "prerelease": False, "published_at": "2026-11-20T00:00:00Z",
         "body": "notes", "assets": [{"name": "Madeira.ipa", "size": 123,
                                      "browser_download_url": "https://dl/madeira-0.3.0.ipa"}]},
    ],
    "ARMSX2/ARMSX3": [
        {"tag_name": "1.5", "draft": False, "prerelease": False, "published_at": "2026-11-24T00:00:00Z",
         "body": "android", "assets": [{"name": "ARMSX3.apk", "size": 1, "browser_download_url": "https://dl/a.apk"}]},
        {"tag_name": "ios-0.0.4", "draft": False, "prerelease": True, "published_at": "2026-11-23T00:00:00Z",
         "body": "ios", "assets": [{"name": "ARMSX3-0.0.4.ipa", "size": 456,
                                    "browser_download_url": "https://dl/armsx3-0.0.4.ipa"}]},
    ],
    "nobody/noipa": [
        {"tag_name": "v1", "draft": False, "prerelease": False, "published_at": "x", "body": "",
         "assets": [{"name": "thing.zip", "size": 1, "browser_download_url": "https://dl/x.zip"}]},
    ],
}
IPAS = {
    "https://dl/madeira-0.3.0.ipa": fake_ipa("com.willfaust.madeira", "0.3.0"),
    "https://dl/armsx3-0.0.4.ipa": fake_ipa("com.armsx3.ios", "0.0.4"),
}
SOURCE = {"apps": [
    {"name": "StikDebug", "bundleIdentifier": "com.stik.stikdebug", "versions": [{"version": "3.2.2"}]},
    {"name": "SomethingElse", "bundleIdentifier": "com.other", "versions": [{"version": "1"}]},
]}


class FakeNet:
    def __init__(self):
        self.downloads = []

    def get_json(self, url, api=False):
        if url.startswith("https://api.github.com/repos/"):
            repo = url.split("/repos/")[1].split("/releases")[0]
            return RELEASES[repo]
        if url == "https://src.example/apps.json":
            return SOURCE
        if url == "https://down.example/apps.json":
            raise OSError("down")
        raise AssertionError(url)

    def http_get(self, url, api=False, timeout=60):
        self.downloads.append(url)
        return IPAS[url]


CONFIG = {
    "feed": {"name": "Test", "identifier": "t"},
    "source": [{"name": "Stik", "url": "https://src.example/apps.json", "only": ["StikDebug"]}],
    "github": [
        {"name": "Madeira", "repo": "willfaust/Madeira", "asset": r"\.ipa$"},
        {"name": "ARMSX3", "repo": "ARMSX2/ARMSX3", "asset": r"\.ipa$", "tag_prefix": "ios-", "prerelease": True},
    ],
}


class BuildTests(unittest.TestCase):
    def run_build(self, config, previous=None):
        net = FakeNet()
        with mock.patch.object(build_feed, "get_json", net.get_json), \
             mock.patch.object(build_feed, "http_get", net.http_get):
            source, errors = build_feed.build(config, previous)
        return source, errors, net

    def test_builds_all_apps(self):
        source, errors, _ = self.run_build(CONFIG)
        self.assertEqual(errors, [])
        names = [a["name"] for a in source["apps"]]
        self.assertEqual(names, ["StikDebug", "Madeira", "ARMSX3"])

    def test_reads_bundle_id_from_ipa(self):
        source, _, _ = self.run_build(CONFIG)
        madeira = next(a for a in source["apps"] if a["name"] == "Madeira")
        self.assertEqual(madeira["bundleIdentifier"], "com.willfaust.madeira")
        self.assertEqual(madeira["versions"][0]["version"], "0.3.0")
        self.assertEqual(madeira["versions"][0]["minOSVersion"], "17.4")
        self.assertEqual(madeira["downloadURL"], "https://dl/madeira-0.3.0.ipa")

    def test_tag_prefix_skips_android_release(self):
        source, _, _ = self.run_build(CONFIG)
        armsx3 = next(a for a in source["apps"] if a["name"] == "ARMSX3")
        self.assertEqual(armsx3["versions"][0]["version"], "0.0.4")
        self.assertEqual(armsx3["bundleIdentifier"], "com.armsx3.ios")

    def test_cache_avoids_redownload(self):
        first, _, _ = self.run_build(CONFIG)
        _, _, net = self.run_build(CONFIG, previous=first)
        self.assertEqual(net.downloads, [])

    def test_missing_ipa_is_reported_not_fatal(self):
        cfg = dict(CONFIG, github=[{"name": "NoIPA", "repo": "nobody/noipa", "asset": r"\.ipa$"}])
        source, errors, _ = self.run_build(cfg)
        self.assertEqual(len(errors), 1)
        self.assertIn("NoIPA", errors[0])
        self.assertEqual([a["name"] for a in source["apps"]], ["StikDebug"])

    def test_source_outage_keeps_last_good_copy(self):
        cfg = dict(CONFIG, source=[{"name": "Down", "url": "https://down.example/apps.json"}], github=[])
        previous = {"apps": [{"name": "Kept", "bundleIdentifier": "k", "_origin": "https://down.example/apps.json"}]}
        source, errors, _ = self.run_build(cfg, previous)
        self.assertEqual([a["name"] for a in source["apps"]], ["Kept"])
        self.assertEqual(len(errors), 1)

    def test_output_is_valid_altstore_shape(self):
        source, _, _ = self.run_build(CONFIG)
        json.dumps(source)
        for a in source["apps"]:
            self.assertIn("bundleIdentifier", a)
            self.assertTrue(a["versions"][0].get("version"))


if __name__ == "__main__":
    unittest.main()
