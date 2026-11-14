#!/usr/bin/env python3
"""
YouTube Channel & Video Extractor for EnglishCraft
Cross-platform (Windows, macOS, Linux) YouTube extractor leveraging yt-dlp.
Supports auto-installing yt-dlp, fetching video lists with covers, extracting subtitles,
and seamlessly exporting to WordTower (词塔) and vocab pipelines.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# Ensure UTF-8 output on Windows console (cmd.exe, PowerShell) to prevent GBK UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def get_ytdlp_command(auto_install: bool = True) -> list[str]:
    """
    Find yt-dlp executable or Python module command.
    If not installed and auto_install is True, automatically installs it via pip.
    Returns a list of command tokens, e.g. ['/path/to/yt-dlp'] or [sys.executable, '-m', 'yt_dlp'].
    """
    # 1. Check custom environment variable
    override = os.environ.get("YTDLP_PATH")
    if override and os.path.isfile(override) and os.access(override, os.X_OK):
        return [override]

    # 2. Check system PATH
    found = shutil.which("yt-dlp")
    if found:
        return [found]

    # 3. Check common platform-specific locations
    common_paths = []
    if os.name == "nt":  # Windows
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        app_data = os.environ.get("APPDATA", "")
        user_profile = os.environ.get("USERPROFILE", "")
        common_paths.extend([
            os.path.join(local_app_data, "Microsoft", "WinGet", "Links", "yt-dlp.exe"),
            os.path.join(local_app_data, "Programs", "yt-dlp", "yt-dlp.exe"),
            os.path.join(user_profile, "scoop", "shims", "yt-dlp.exe"),
            os.path.join(os.path.dirname(sys.executable), "Scripts", "yt-dlp.exe"),
            os.path.join(app_data, "Python", "Scripts", "yt-dlp.exe"),
        ])
    else:  # macOS / Linux
        common_paths.extend([
            "/opt/homebrew/bin/yt-dlp",
            "/usr/local/bin/yt-dlp",
            os.path.expanduser("~/.local/bin/yt-dlp"),
            os.path.join(os.path.dirname(sys.executable), "yt-dlp"),
        ])

    for p in common_paths:
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return [p]

    # 4. Check if yt_dlp Python module is installed
    try:
        res = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--version"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
        if res.returncode == 0:
            return [sys.executable, "-m", "yt_dlp"]
    except Exception:
        pass

    # 5. Not found - Attempt automatic self-healing installation
    if auto_install:
        print("\n🔧 [EnglishCraft] 'yt-dlp' is required but not found.", file=sys.stderr)
        print("   Attempting automatic installation via pip ...", file=sys.stderr)
        try:
            install_cmd = [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"]
            install_res = subprocess.run(install_cmd, check=False)
            if install_res.returncode == 0:
                print("✅ [EnglishCraft] 'yt-dlp' successfully installed!\n", file=sys.stderr)
                # Re-check via module or PATH
                found = shutil.which("yt-dlp")
                if found:
                    return [found]
                return [sys.executable, "-m", "yt_dlp"]
            else:
                print("⚠️  Automatic pip install exited with non-zero status.", file=sys.stderr)
        except Exception as exc:
            print(f"⚠️  Automatic installation failed: {exc}", file=sys.stderr)

    # 6. Failure guidance
    print(
        "\n❌ [EnglishCraft YouTube] Error: 'yt-dlp' is not available.\n"
        "   Please install yt-dlp using one of the following:\n",
        file=sys.stderr,
    )
    if os.name == "nt":
        print("     - Windows (pip):    python -m pip install -U yt-dlp", file=sys.stderr)
        print("     - Windows (winget): winget install yt-dlp", file=sys.stderr)
    elif sys.platform == "darwin":
        print("     - macOS (Homebrew): brew install yt-dlp", file=sys.stderr)
        print("     - macOS (pip):      pip3 install -U yt-dlp", file=sys.stderr)
    else:
        print("     - Linux:            pip install -U yt-dlp", file=sys.stderr)
    print("", file=sys.stderr)
    sys.exit(1)


def format_duration(seconds: float | int | None) -> str:
    """Format duration in seconds to MM:SS or HH:MM:SS."""
    if seconds is None:
        return "--:--"
    total_sec = int(seconds)
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    secs = total_sec % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def normalize_youtube_target(target: str) -> str:
    """
    Normalize user input into a valid yt-dlp target.
    Supports:
      - @username, @handle -> https://www.youtube.com/@handle/videos
      - username (without @) -> https://www.youtube.com/@username/videos
      - https://www.youtube.com/@handle -> append /videos
      - https://www.youtube.com/channel/UC... -> append /videos
      - https://www.youtube.com/playlist?list=...
      - https://www.youtube.com/watch?v=...
    """
    raw = target.strip()

    if raw.startswith("http://") or raw.startswith("https://"):
        if "youtube.com/@" in raw and not raw.endswith("/videos"):
            return raw.rstrip("/") + "/videos"
        if "youtube.com/channel/UC" in raw and not raw.endswith("/videos"):
            return raw.rstrip("/") + "/videos"
        return raw

    if raw.startswith("UC") and len(raw) == 24:
        return f"https://www.youtube.com/channel/{raw}/videos"

    handle = raw if raw.startswith("@") else f"@{raw}"
    return f"https://www.youtube.com/{handle}/videos"


def fetch_channel_videos(
    target: str,
    limit: int = 10,
    start: int = 1,
    proxy: str | None = None,
) -> dict[str, Any]:
    """Fetch video entries from a YouTube channel/playlist using yt-dlp."""
    ytdlp_cmd = get_ytdlp_command()
    url = normalize_youtube_target(target)

    end = start + limit - 1
    cmd = (
        ytdlp_cmd
        + [
            "--flat-playlist",
            "--no-warnings",
            "--no-update",
            "--playlist-start",
            str(start),
            "--playlist-end",
            str(end),
            "-J",
            url,
        ]
    )

    if proxy:
        cmd.extend(["--proxy", proxy])

    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        err_msg = (exc.stderr or "").strip() or (exc.stdout or "").strip()
        print(f"❌ yt-dlp failed to fetch {url}:\n{err_msg}", file=sys.stderr)
        raise RuntimeError(f"Failed to fetch videos from {url}") from exc

    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("yt-dlp returned invalid JSON output") from exc

    channel_name = data.get("channel") or data.get("uploader") or target
    channel_url = data.get("channel_url") or data.get("uploader_url") or url
    followers = data.get("channel_follower_count")

    entries = []
    current_idx = start
    for entry in data.get("entries", []) or []:
        if not entry or not isinstance(entry, dict):
            continue
        vid_id = entry.get("id")
        if not vid_id:
            continue
        title = entry.get("title") or "Untitled Video"
        duration_sec = entry.get("duration")
        duration_str = format_duration(duration_sec)
        vid_url = entry.get("url") or f"https://www.youtube.com/watch?v={vid_id}"

        thumbnails = entry.get("thumbnails") or []
        cover_url = ""
        if thumbnails and isinstance(thumbnails, list):
            valid_thumbs = [t for t in thumbnails if isinstance(t, dict) and t.get("url")]
            if valid_thumbs:
                cover_url = valid_thumbs[-1].get("url", "")

        entries.append({
            "index": current_idx,
            "id": vid_id,
            "title": title,
            "duration_seconds": duration_sec,
            "duration": duration_str,
            "url": vid_url,
            "cover": cover_url,
            "view_count": entry.get("view_count"),
        })
        current_idx += 1

    return {
        "channel_name": channel_name,
        "channel_url": channel_url,
        "followers": followers,
        "target_url": url,
        "total_results": len(entries),
        "entries": entries,
    }


def extract_video_id(video_url_or_id: str) -> str | None:
    """Extract a YouTube video ID from a URL or bare ID."""
    raw = video_url_or_id.strip()
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        return raw if re.fullmatch(r"[A-Za-z0-9_-]{6,}", raw) else None

    parsed = urllib.parse.urlsplit(raw)
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        seg = parsed.path.strip("/").split("/")[0]
        return seg or None
    if "youtube.com" in host or "youtube-nocookie.com" in host:
        qs = urllib.parse.parse_qs(parsed.query)
        if qs.get("v"):
            return qs["v"][0]
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v"}:
            return parts[1]
    return None


def canonical_video_url(video_url_or_id: str) -> tuple[str, str]:
    """Return (canonical watch URL, safe filename ID) for a YouTube video."""
    vid_id = extract_video_id(video_url_or_id)
    if vid_id:
        return f"https://www.youtube.com/watch?v={vid_id}", vid_id
    return video_url_or_id.strip(), "youtube_sub"


def _parse_format_priority(sub_format: str) -> list[str]:
    """Parse comma- or slash-separated subtitle format preferences."""
    tokens = [tok.strip().lower() for tok in re.split(r"[,/]+", sub_format or "srt") if tok.strip()]
    ordered: list[str] = []
    for tok in tokens:
        if tok not in ordered:
            ordered.append(tok)
    return ordered or ["srt"]


def _fetch_video_metadata(
    ytdlp_cmd: list[str],
    vid_url: str,
    proxy: str | None = None,
) -> dict[str, Any] | None:
    """Fetch full yt-dlp metadata JSON for a single video."""
    cmd = ytdlp_cmd + [
        "--no-warnings",
        "--no-update",
        "--skip-download",
        "-J",
        vid_url,
    ]
    if proxy:
        cmd.extend(["--proxy", proxy])
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        data = json.loads(res.stdout)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _is_translated_track(track_url: str) -> bool:
    """Return True when a YouTube timedtext URL uses machine translation (tlang=)."""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(track_url).query)
    return "tlang" in query


def _expand_lang_candidates(lang: str) -> list[str]:
    """Expand requested languages while preserving user priority order."""
    raw_langs = [item.strip() for item in (lang or "en").split(",") if item.strip()]
    expanded: list[str] = []
    for item in raw_langs:
        if item not in expanded:
            expanded.append(item)
        if "-" not in item and f"{item}-orig" not in expanded:
            expanded.append(f"{item}-orig")
    return expanded or ["en", "en-orig"]


def _iter_track_candidates(
    metadata: dict[str, Any],
    langs: list[str],
    formats: list[str],
) -> list[tuple[str, str, str]]:
    """
    Collect ordered (lang, ext, url) subtitle candidates from yt-dlp metadata.
    Prioritizes manual subtitles over auto captions, and non-translated tracks
    (without tlang=) over translated tracks to avoid rate limits and drift.
    """
    buckets = [
        metadata.get("subtitles") or {},
        metadata.get("automatic_captions") or {},
    ]
    primary: list[tuple[str, str, str]] = []
    translated: list[tuple[str, str, str]] = []

    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        for req_lang in langs:
            tracks = bucket.get(req_lang)
            if not isinstance(tracks, list):
                continue
            for fmt in formats:
                for track in tracks:
                    if not isinstance(track, dict):
                        continue
                    if (track.get("ext") or "").lower() != fmt:
                        continue
                    url = track.get("url")
                    if not url:
                        continue
                    item = (req_lang, fmt, url)
                    if _is_translated_track(url):
                        translated.append(item)
                    else:
                        primary.append(item)

    return primary + translated


def _download_track_url(
    url: str,
    dest_path: Path,
    fmt: str,
    proxy: str | None = None,
) -> bool:
    """Download and validate a single subtitle track URL."""
    req = urllib.request.Request(
        url,
        headers={
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
            ),
        },
    )
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=20) as resp:
            raw = resp.read()
    except Exception:
        return False

    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        return False
    if fmt == "json3":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return False
        if not isinstance(parsed, dict) or not isinstance(parsed.get("events"), list) or not parsed["events"]:
            return False

    dest_path.write_bytes(raw)
    return True


def _pick_downloaded_subtitle(
    out_folder: Path,
    safe_id: str,
    formats: list[str],
) -> Path | None:
    """Select the highest-priority downloaded subtitle file matching format preference."""
    for fmt in formats:
        candidates = list(out_folder.glob(f"{safe_id}*.{fmt}"))
        if candidates:
            candidates.sort(key=lambda p: (len(p.name), p.name))
            return candidates[0]
    return None


def download_subtitles_with_meta(
    video_url_or_id: str,
    output_dir: str | Path | None = None,
    lang: str = "en,en-US,en-GB",
    proxy: str | None = None,
    sub_format: str = "srt",
) -> tuple[Path, dict[str, Any]]:
    """
    Download subtitles for a YouTube video in the preferred format order
    (e.g. 'srt' or 'json3,vtt,srt') and return (subtitle_path, video_meta).
    """
    ytdlp_cmd = get_ytdlp_command()
    vid_url, safe_id = canonical_video_url(video_url_or_id)
    formats = _parse_format_priority(sub_format)
    langs = _expand_lang_candidates(lang)

    out_folder = Path(output_dir or "./transcripts").resolve()
    out_folder.mkdir(parents=True, exist_ok=True)

    metadata = _fetch_video_metadata(ytdlp_cmd, vid_url, proxy=proxy)
    video_meta = {
        "video_id": safe_id,
        "video_url": vid_url,
        "title": (metadata or {}).get("title"),
    }

    if metadata:
        for track_lang, fmt, track_url in _iter_track_candidates(metadata, langs, formats):
            clean_lang = re.sub(r"[^A-Za-z0-9_-]+", "_", track_lang.removesuffix("-orig")) or "en"
            dest_path = out_folder / f"{safe_id}.{clean_lang}.{fmt}"
            if _download_track_url(track_url, dest_path, fmt, proxy=proxy):
                return dest_path, video_meta

    # Fallback: let yt-dlp write subtitles directly (try first explicit lang to avoid mass tlang 429s)
    out_template = str(out_folder / f"{safe_id}.%(ext)s")
    fmt_arg = "/".join(formats)
    fallback_langs = [lang, langs[0]] if "," in lang else [lang]
    last_err = ""
    for lang_arg in fallback_langs:
        cmd = (
            ytdlp_cmd
            + [
                "--write-sub",
                "--write-auto-sub",
                "--sub-lang",
                lang_arg,
                "--sub-format",
                fmt_arg,
                "--skip-download",
                "--no-warnings",
                "--no-update",
                "-o",
                out_template,
                vid_url,
            ]
        )
        if proxy:
            cmd.extend(["--proxy", proxy])
        res = subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
        found = _pick_downloaded_subtitle(out_folder, safe_id, formats)
        if found is not None:
            return found, video_meta
        last_err = (res.stderr or "").strip() or (res.stdout or "").strip()

    if last_err:
        raise RuntimeError(f"Failed to download subtitles: {last_err}")
    raise FileNotFoundError(f"No subtitle file was generated in {out_folder}")


def download_subtitles(
    video_url_or_id: str,
    output_dir: str | Path | None = None,
    lang: str = "en,en-US,en-GB",
    proxy: str | None = None,
    sub_format: str = "srt",
) -> Path:
    """Download English subtitles for a video and return the subtitle file path."""
    sub_path, _ = download_subtitles_with_meta(
        video_url_or_id,
        output_dir=output_dir,
        lang=lang,
        proxy=proxy,
        sub_format=sub_format,
    )
    return sub_path


def print_human_table(result: dict[str, Any], show_covers: bool = False):
    """Print results in a clean terminal list."""
    ch_name = result.get("channel_name", "YouTube Channel")
    followers = result.get("followers")
    follower_str = f" ({followers:,} subscribers)" if followers else ""
    entries = result.get("entries", [])

    print(f"\n📺  Channel: {ch_name}{follower_str}")
    print(f"🔗  URL: {result.get('target_url')}")
    print(f"📊  Showing {len(entries)} videos:\n")

    for item in entries:
        idx = item["index"]
        dur = item["duration"]
        title = item["title"]
        url = item["url"]
        cover = item["cover"]

        print(f"  [{idx:2d}] ⏱️ [{dur}]  {title}")
        print(f"       🔗 {url}")
        if show_covers and cover:
            print(f"       🖼️  Cover: {cover}")
        print()


def print_markdown(result: dict[str, Any]):
    """Print results in rich Markdown format with image thumbnails and links."""
    ch_name = result.get("channel_name", "YouTube Channel")
    followers = result.get("followers")
    follower_str = f" ({followers:,} subscribers)" if followers else ""
    entries = result.get("entries", [])

    print(f"\n### 📺 [{ch_name}]({result.get('target_url')}){follower_str}\n")
    print("| # | 封面 | 视频标题 | 时长 | 链接 |")
    print("|:---:|:---:|:---|:---:|:---:|")

    for item in entries:
        idx = item["index"]
        dur = item["duration"]
        title = item["title"].replace("|", "\\|")
        url = item["url"]
        cover = item["cover"]
        img_md = f'<img src="{cover}" width="120" style="border-radius:4px;" />' if cover else "-"
        print(f"| **{idx}** | {img_md} | [{title}]({url}) | `{dur}` | [观看]({url}) |")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Fetch YouTube channel video lists, download subtitles, or import to WordTower."
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="Subcommand to execute")

    # list subcommand
    list_parser = subparsers.add_parser("list", help="List videos for a YouTube channel or handle")
    list_parser.add_argument("target", help="YouTube handle (e.g. @TEDEd), username, or channel URL")
    list_parser.add_argument("-n", "--limit", type=int, default=10, help="Number of videos to list (default: 10)")
    list_parser.add_argument("-s", "--start", type=int, default=1, help="Starting video index (default: 1)")
    list_parser.add_argument("--json", action="store_true", help="Output results in JSON format")
    list_parser.add_argument("--markdown", action="store_true", help="Output results in rich Markdown format")
    list_parser.add_argument("--show-covers", action="store_true", help="Display image cover URLs in terminal")
    list_parser.add_argument("--proxy", help="HTTP/HTTPS proxy (e.g. http://127.0.0.1:7890)")

    # subtitles subcommand
    sub_parser = subparsers.add_parser("subtitles", help="Download subtitles for a YouTube video")
    sub_parser.add_argument("video", help="YouTube video URL or Video ID")
    sub_parser.add_argument("-o", "--output-dir", default="./transcripts", help="Directory to save subtitle file")
    sub_parser.add_argument("--lang", default="en,en-US,en-GB", help="Subtitle language (default: en,en-US,en-GB)")
    sub_parser.add_argument(
        "--format",
        default="srt",
        help="Subtitle format or priority list, e.g. srt, vtt, json3, or json3,vtt,srt (default: srt)",
    )
    sub_parser.add_argument("--proxy", help="HTTP/HTTPS proxy")
    sub_parser.add_argument("--json", action="store_true", help="Output result as JSON")

    # wordtower subcommand
    wt_parser = subparsers.add_parser("wordtower", help="Download subtitles and import video into WordTower")
    wt_parser.add_argument("video", help="YouTube video URL or Video ID")
    wt_parser.add_argument("-t", "--title", help="Course title for WordTower (defaults to video title)")
    wt_parser.add_argument("-o", "--output-dir", default="./transcripts", help="Directory to save subtitle file")
    wt_parser.add_argument("--lang", default="en,en-US,en-GB", help="Subtitle language (default: en,en-US,en-GB)")
    wt_parser.add_argument(
        "--format",
        default="json3,vtt,srt",
        help="Subtitle format priority for WordTower import (default: json3,vtt,srt)",
    )
    wt_parser.add_argument("--preview-only", action="store_true", help="Preview only on WordTower without submit")
    wt_parser.add_argument("--proxy", help="HTTP/HTTPS proxy")

    # Support shorthand if no subcommand given: 'fetch_youtube.py @TEDEd'
    if len(sys.argv) > 1 and sys.argv[1] not in ("list", "subtitles", "wordtower", "-h", "--help"):
        # Synthesize 'list' command
        sys.argv.insert(1, "list")

    args = parser.parse_args()

    if args.subcommand == "list":
        result = fetch_channel_videos(
            target=args.target,
            limit=args.limit,
            start=args.start,
            proxy=args.proxy,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.markdown:
            print_markdown(result)
        else:
            print_human_table(result, show_covers=args.show_covers)

    elif args.subcommand == "subtitles":
        try:
            sub_path = download_subtitles(
                video_url_or_id=args.video,
                output_dir=args.output_dir,
                lang=args.lang,
                proxy=args.proxy,
                sub_format=args.format,
            )
            if args.json:
                print(json.dumps({
                    "status": "success",
                    "video": args.video,
                    "format": sub_path.suffix.lstrip("."),
                    "subtitles_path": str(sub_path),
                }, indent=2))
            else:
                print(f"\n✅ Subtitles saved to: {sub_path}\n")
        except Exception as exc:
            print(f"\n❌ Error downloading subtitles: {exc}\n", file=sys.stderr)
            sys.exit(1)

    elif args.subcommand == "wordtower":
        # Prefer JSON3 subtitles so WordTower preserves word-level timestamps when available
        try:
            print(f"📥 Downloading subtitles ({args.format}) for {args.video} ...")
            sub_path, video_meta = download_subtitles_with_meta(
                args.video,
                output_dir=args.output_dir,
                lang=args.lang,
                proxy=args.proxy,
                sub_format=args.format,
            )
            print(f"✓ Subtitles downloaded: {sub_path}")
        except Exception as exc:
            print(f"❌ Failed to download subtitles: {exc}", file=sys.stderr)
            sys.exit(1)

        # Call exporters/wordtower.py
        exporter_script = Path(__file__).resolve().parent / "exporters" / "wordtower.py"
        if not exporter_script.is_file():
            print(f"❌ exporters/wordtower.py not found at {exporter_script}", file=sys.stderr)
            sys.exit(1)

        vid_url = video_meta.get("video_url") or canonical_video_url(args.video)[0]
        course_title = args.title or video_meta.get("title")
        cmd = [
            sys.executable,
            str(exporter_script),
            "youtube",
            vid_url,
            "--subtitles",
            str(sub_path),
        ]
        if course_title:
            cmd.extend(["--title", course_title])
        if args.preview_only:
            cmd.append("--preview-only")

        print("🗼 Launching WordTower import pipeline...")
        subprocess.run(cmd, check=True)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
