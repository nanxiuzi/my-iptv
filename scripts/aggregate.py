#!/usr/bin/env python3
"""Aggregate public IPTV playlists into live.m3u / international.m3u.

Only the Python standard library is used, so it runs anywhere (including a
bare GitHub Actions runner) with no pip install step.

Behaviour:
  * Reads sources.json -> {"sources": [{"name", "url", "intl"?}], "check": bool}
  * Fetches every upstream m3u, parses #EXTINF entries leniently (handles both
    `#EXTINF:-1 attr="x",Name` and the malformed `#EXTINF:01,a="x",Name` style).
  * Drops unsupported schemes (rtp/udp/rtsp/igmp) that no public player can use.
  * De-duplicates on (normalised name, stream url).
  * Optionally probes each stream for a short timeout and sorts reachable
    channels first (check=true).
  * Writes live.m3u (domestic) and international.m3u (intl-tagged sources).

Usage:
    python scripts/aggregate.py            # read sources.json in repo root
    python scripts/aggregate.py --check    # force speed checking on
    python scripts/aggregate.py --no-check # force speed checking off
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES_FILE = ROOT / "sources.json"
LIVE_OUT = ROOT / "live.m3u"
INTL_OUT = ROOT / "international.m3u"

EPG_URL = "http://epg.51zmt.top:8000/e.xml"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

ALLOWED_SCHEMES = ("http://", "https://")

# Attributes we care about preserving from the upstream #EXTINF line.
KEEP_ATTRS = ("tvg-id", "tvg-name", "tvg-logo", "group-title")

# Names that almost always point at dead / placeholder lines.
JUNK_NAME_PATTERNS = (
    re.compile(r"^\s*$"),
    re.compile(r"^\d+$"),
    re.compile(r"(?i)^(test|demo|sample|备用|测试)"),
    # Upstreams sometimes ship a bogus "last updated" entry as a channel.
    re.compile(r"(?i)^\d{4}-\d{2}-\d{2}"),
    re.compile(r"(?i)(更新时间|更新日期|数据来源|announcement)"),
)

# Attr values that mark a placeholder entry rather than a real channel.
JUNK_TVG_ID = {"更新时间", "update", "updated"}

PROBE_TIMEOUT = 4.0
PROBE_WORKERS = 24


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def fetch(url: str, timeout: float = 30.0) -> str:
    """Download a playlist, transparently handling .gz and gzip Content-Encoding."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        encoding = (resp.headers.get("Content-Encoding") or "").lower()
    if url.endswith(".gz") or encoding == "gzip":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    for enc in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)\s*=\s*"([^"]*)"')


def _split_extinf(line: str) -> tuple[dict[str, str], str]:
    """Parse an #EXTINF line into (attributes, display name).

    Tolerates the common malformed variant where attributes appear after the
    duration comma, e.g. `#EXTINF:01,group-title="央视" tvg-name="CCTV1",央视综合`.
    """
    body = line.split(":", 1)[1] if ":" in line else line

    attrs: dict[str, str] = {}
    # Take everything before the *last* comma-like boundary as attributes; the
    # final comma-delimited chunk (after the last quote) is the display name.
    last_quote = body.rfind('"')
    if last_quote != -1:
        attr_part = body[: last_quote + 1]
        name_part = body[last_quote + 1 :].lstrip(" ,")
        for key, val in ATTR_RE.findall(attr_part):
            attrs[key.strip().lower()] = val.strip()
    else:
        # No attributes at all: `#EXTINF:-1,Name`
        name_part = body.split(",", 1)[1] if "," in body else body
        name_part = name_part.strip()

    name = name_part.strip()
    if not name:
        name = attrs.get("tvg-name", "").strip()
    return attrs, name


def parse_playlist(text: str, source_name: str) -> list[dict]:
    """Return a list of channel dicts parsed from an m3u/m3u8 document."""
    entries: list[dict] = []
    pending: dict[str, str] | None = None
    order = 0

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            attrs, name = _split_extinf(line)
            pending = {"attrs": attrs, "name": name}  # type: ignore[assignment]
            continue
        if line.startswith("#"):
            # Other directives (#EXTVLCOPT, #KODIPROP, ...) are ignored.
            continue

        # A non-comment line is a stream URL; only emit if we had an #EXTINF.
        if pending is None:
            continue
        url = line
        attrs = pending["attrs"]  # type: ignore[union-attr]
        name = pending["name"]  # type: ignore[union-attr]
        pending = None

        if not url.lower().startswith(ALLOWED_SCHEMES):
            continue
        if not name or any(p.search(name) for p in JUNK_NAME_PATTERNS):
            continue
        if attrs.get("tvg-id", "").strip().lower() in JUNK_TVG_ID:
            continue

        order += 1
        entries.append(
            {
                "name": name,
                "url": url,
                "attrs": attrs,
                "source": source_name,
                "order": order,
            }
        )
    return entries


# --------------------------------------------------------------------------- #
# Normalisation / de-dup
# --------------------------------------------------------------------------- #
QUALITY_RE = re.compile(r"(?i)[\(\[]?(1080|720|576|480|360|240)p?[\)\]]?$")
LINE_SUFFIX_RE = re.compile(r"(?i)\s*[-_]?\s*线路\s*\d+\s*$")


def base_key(name: str) -> str:
    """Channel identity used for de-duplication (ignores quality/line suffix)."""
    k = name.strip().lower()
    k = QUALITY_RE.sub("", k)
    k = LINE_SUFFIX_RE.sub("", k)
    k = re.sub(r"[\s\-_]+", "", k)
    return k


def dedup(entries: list[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for e in entries:
        key = (base_key(e["name"]), e["url"])
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


# --------------------------------------------------------------------------- #
# Probing
# --------------------------------------------------------------------------- #
def probe(entry: dict) -> bool:
    """Short HEAD/GET probe; True if the stream looks reachable."""
    url = entry["url"]
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:
            code = resp.getcode()
            if not (200 <= code < 400):
                return False
            # Read a small chunk; some servers 200 then immediately close.
            chunk = resp.read(2048)
            if chunk and b"#EXTM3U" in chunk[:64]:
                return True
            return bool(chunk) or code == 200
    except Exception:
        return False


def apply_check(entries: list[dict]) -> list[dict]:
    print(f"  probing {len(entries)} streams (timeout {PROBE_TIMEOUT}s)...", flush=True)
    with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
        results = list(pool.map(probe, entries))
    reachable = [e for e, ok in zip(entries, results) if ok]
    dead = [e for e, ok in zip(entries, results) if not ok]
    print(
        f"  reachable: {len(reachable)} / dead: {len(dead)} "
        f"(dead kept but sorted last)",
        flush=True,
    )
    return reachable + dead


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
def render_entry(e: dict) -> str:
    attrs = e["attrs"]
    parts = ["#EXTINF:-1"]
    for key in KEEP_ATTRS:
        val = attrs.get(key, "")
        if key == "tvg-id" and not val:
            continue
        if key == "tvg-name" and not val:
            val = e["name"]
        if key == "group-title" and not val:
            val = "其他"
        if not val:
            continue
        parts.append(f'{key}="{val}"')
    header = " ".join(parts) + f",{e['name']}"
    return f"{header}\n{e['url']}"


def write_m3u(path: Path, entries: list[dict]) -> None:
    lines = [f'#EXTM3U x-tvg-url="{EPG_URL}"']
    lines.extend(render_entry(e) for e in entries)
    text = "\n".join(lines) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
    print(f"  wrote {path.name}: {len(entries)} channels", flush=True)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    if not SOURCES_FILE.exists():
        sys.exit(f"sources.json not found at {SOURCES_FILE}")
    return json.loads(SOURCES_FILE.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", dest="check", action="store_true", default=None)
    ap.add_argument("--no-check", dest="check", action="store_false")
    args = ap.parse_args()

    cfg = load_config()
    sources = cfg.get("sources", [])
    do_check = cfg.get("check", False) if args.check is None else args.check

    if not sources:
        sys.exit("sources.json has no 'sources' entries")

    domestic: list[dict] = []
    international: list[dict] = []
    failed: list[str] = []

    for src in sources:
        name = src.get("name") or src["url"]
        url = src["url"]
        try:
            print(f"-> {name}: {url}", flush=True)
            text = fetch(url)
            parsed = parse_playlist(text, name)
            print(f"   parsed {len(parsed)} entries", flush=True)
        except Exception as exc:  # noqa: BLE001 - keep going on any single failure
            print(f"   FAILED: {exc}", flush=True)
            failed.append(name)
            continue
        target = international if src.get("intl") else domestic
        target.extend(parsed)

    domestic = dedup(domestic)
    international = dedup(international)

    if not domestic and not international:
        sys.exit("no entries parsed from any source; aborting without overwriting files")

    if do_check:
        print("checking domestic streams", flush=True)
        domestic = apply_check(domestic)
        print("checking international streams", flush=True)
        international = apply_check(international)

    write_m3u(LIVE_OUT, domestic)
    write_m3u(INTL_OUT, international)

    if failed:
        print(f"WARNING: {len(failed)} source(s) failed: {', '.join(failed)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())