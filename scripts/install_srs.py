"""Download a pinned official Windows SRS release into this workspace only."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VERSION = "v8.0-d3"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=DEFAULT_VERSION)
    args = parser.parse_args()
    version = args.version
    if not version.startswith("v") or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for char in version):
        parser.error("Invalid release tag")
    destination = ROOT / "tools" / "srs"
    if (destination / "objs" / "srs.exe").is_file():
        print(f"SRS already exists: {destination / 'objs' / 'srs.exe'}")
        return 0
    asset_name = f"SRS-Windows-x86_64-{version[1:]}.zip"

    def request(url: str):
        return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "SRS-Desktop-Manager"}), timeout=60)

    with request(f"https://api.github.com/repos/ossrs/srs/releases/tags/{version}") as response:
        release = json.load(response)
    asset = next((item for item in release["assets"] if item["name"] == asset_name), None)
    if asset is None:
        raise RuntimeError(f"Release {version} does not contain {asset_name}")
    url = asset["browser_download_url"]
    downloads = ROOT / "tools" / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    archive = downloads / asset_name
    print(f"Downloading {url}", flush=True)
    temporary = archive.with_suffix(".part")
    with request(url) as response, temporary.open("wb") as output:
        shutil.copyfileobj(response, output)
    with temporary.open("rb") as downloaded:
        digest = hashlib.file_digest(downloaded, "sha256").hexdigest()
    expected = asset.get("digest")
    if expected and expected != f"sha256:{digest}":
        raise RuntimeError("Official asset SHA256 verification failed")
    temporary.replace(archive)
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        roots = {Path(item.filename).parts[0] for item in members if Path(item.filename).parts}
        prefix = next(iter(roots)) if len(roots) == 1 else None
        # Releases can contain a top-level directory or directly objs/conf.
        strip_prefix = bool(prefix and prefix not in ("objs", "conf") and not any(item.filename == "srs.exe" for item in members))
        for item in members:
            relative = Path(item.filename)
            if strip_prefix:
                relative = Path(*relative.parts[1:])
            target = (destination / relative).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise RuntimeError(f"Unsafe archive path: {item.filename}")
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(item) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    if not (destination / "objs" / "srs.exe").is_file():
        raise RuntimeError("Archive did not contain objs/srs.exe; select the executable manually in the UI.")
    (destination / "release.json").write_text(json.dumps({"version": version, "url": url,
        "sha256": digest, "official_digest_checked": bool(expected)}, indent=2), encoding="utf-8")
    print(f"Installed {version}: {destination / 'objs' / 'srs.exe'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"SRS install failed: {error}", file=sys.stderr)
        raise SystemExit(1)
