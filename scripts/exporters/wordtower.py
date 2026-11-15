#!/usr/bin/env python3
"""Prepare corpus for WordTower MCP or import through the official gateway helper."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlsplit
import uuid


def canonical_youtube_url(value):
    raw = value.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", raw):
        return "https://www.youtube.com/watch?v=" + raw
    try:
        parsed = urlsplit(raw)
        if (parsed.scheme not in {"http", "https"} or parsed.hostname not in {
                "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
                "youtube-nocookie.com", "www.youtube-nocookie.com"}
                or parsed.username or parsed.password or parsed.port not in {None, 80, 443}):
            raise ValueError()
        parts = parsed.path.strip("/").split("/")
        if parsed.hostname == "youtu.be" and len(parts) == 1:
            identity = parts[0]
        elif parsed.path == "/watch":
            identity = parse_qs(parsed.query).get("v", [""])[0]
        elif len(parts) == 2 and parts[0] in {"embed", "shorts", "live", "v"}:
            identity = parts[1]
        else:
            raise ValueError()
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", identity):
            raise ValueError()
    except ValueError:
        raise ValueError("Provide one valid YouTube video URL or 11-character ID") from None
    return "https://www.youtube.com/watch?v=" + identity


def find_wordtower_request_script():
    """Prefer an explicitly selected active Skill over guessed client cache paths."""
    override = os.environ.get("WORDTOWER_REQUEST_SCRIPT")
    skill_dir = os.environ.get("WORDTOWER_SKILL_DIR")
    explicit = override or (str(Path(skill_dir).expanduser() / "scripts/request.py") if skill_dir else None)
    candidates = [Path(explicit).expanduser()] if explicit else [
        Path(__file__).resolve().parents[3] / "wordtower/scripts/request.py",
        Path.cwd() / ".agents/skills/wordtower/scripts/request.py",
        Path.cwd() / ".claude/skills/wordtower/scripts/request.py",
        Path.cwd() / "skills/wordtower/scripts/request.py",
    ]
    if not explicit:
        for root in ("~/.agents/skills", "~/.codex/skills", "~/.claude/skills", "~/.gemini/skills",
                     "~/.gemini/config/skills", "~/.gemini/antigravity/skills", "~/.workbuddy/skills", ".workbuddy/skills"):
            candidates.append(Path(root).expanduser() / "wordtower/scripts/request.py")
    for path in candidates:
        config = path.parent.parent / "config.json"
        if path.is_file() and config.is_file():
            source = path.read_text(encoding="utf-8")
            if all(flag in source for flag in ("--subtitles", "WORDTOWER_API_KEY", ".json3", "--prepare-only")):
                return str(path.resolve())
    if explicit:
        raise RuntimeError("Selected WordTower Skill needs current request.py and its companion config.json")
    return None


def get_required_request_script():
    path = find_wordtower_request_script()
    if not path:
        raise RuntimeError("Install/update the optional WordTower Skill, or set WORDTOWER_SKILL_DIR to the active "
                           "Skill directory (including Claude plugin installations). You can also set WORDTOWER_REQUEST_SCRIPT.")
    return path


def prepare_env():
    """Keep existing local credential compatibility; never print credential values."""
    env = os.environ.copy()
    if not env.get("WORDTOWER_API_KEY") and not env.get("WORDS_API_KEY"):
        for path in (Path.cwd() / ".env", Path.home() / ".env"):
            if path.is_file():
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.startswith("WORDTOWER_API_KEY="):
                        env["WORDTOWER_API_KEY"] = line.split("=", 1)[1].strip().strip('"\'')
                        break
            if env.get("WORDTOWER_API_KEY"):
                break
    token = env.get("WORDTOWER_API_KEY") or env.get("WORDS_API_KEY") or ""
    if not re.fullmatch(r"wtk-[A-Za-z0-9_-]{24}", token):
        raise RuntimeError("Configure WORDTOWER_API_KEY locally in your AI client; create it in WordTower Account → Skill & Token")
    env["WORDTOWER_API_KEY"] = token
    return env


def run_helper(script_path, arguments, env):
    result = subprocess.run([sys.executable, str(script_path), *map(str, arguments)], env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=300)
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        raise RuntimeError("WordTower helper failed or returned invalid JSON; preserve the recovery file") from None
    if result.returncode or not isinstance(payload, dict) or payload.get("error"):
        failure = payload.get("error") if isinstance(payload, dict) else None
        code = failure.get("code", "REQUEST_FAILED") if isinstance(failure, dict) else "REQUEST_FAILED"
        # Only report a bounded protocol code, not arbitrary stderr, URLs or content.
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z_]{1,64}", code):
            code = "REQUEST_FAILED"
        raise RuntimeError("WordTower request failed: " + code + "; preserve the recovery file")
    return payload


def run_request_json(script_path, payload_dict, env):
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as target:
        json.dump(payload_dict, target, ensure_ascii=False)
        temporary = target.name
    try:
        return run_helper(script_path, [temporary], env)
    finally:
        Path(temporary).unlink(missing_ok=True)


def validate_youtube_preview(response, youtube_url):
    data = response.get("data") or {}
    count = data.get("subtitle_count")
    try:
        same_video = canonical_youtube_url(data.get("source_url", "")) == canonical_youtube_url(youtube_url)
    except (ValueError, AttributeError):
        same_video = False
    if (data.get("media_type") != "youtube" or not same_video or not data.get("preview_id")
            or not isinstance(count, int) or isinstance(count, bool) or count <= 0):
        raise RuntimeError("YouTube preview lacks the requested video's verified subtitle timeline; do not fall back to text")
    timings = data.get("word_timing_count")
    if isinstance(timings, int) and not isinstance(timings, bool) and timings > 0:
        print(f"✓ Verified {count} subtitle cues and {timings} word timings")
    else:
        print(f"✓ Verified {count} subtitle cues (segment timing only)")


def check_capabilities(script_path, kind, env, preview_only=False):
    response = run_request_json(script_path, {"api_name": "/account/capabilities"}, env)
    data = response.get("data") or {}
    required = {"imports:epub" if kind == "epub" else "imports:text"}
    if not preview_only:
        required.add("imports:read")
    if not required <= set(data.get("scopes") or []):
        raise RuntimeError("WordTower Token lacks the required import/status scopes")
    usage = data.get("import_usage") or {}
    print("Import usage: " + json.dumps(usage, ensure_ascii=False))
    print("Text parse quota: " + json.dumps(data.get("text_parse"), ensure_ascii=False))
    remaining = usage.get("remaining")
    if not preview_only and isinstance(remaining, (int, float)) and remaining <= 0:
        raise RuntimeError("Daily WordTower import limit reached; use resume for an already accepted task")
    return data


def connection_identity(script_path, env):
    config = json.loads((Path(script_path).parent.parent / "config.json").read_text(encoding="utf-8"))
    # Bind recovery to the selected destination and credential, never store the credential itself.
    return hashlib.sha256(json.dumps([config["api_origin"].rstrip("/"), config["gateway_path"],
        env["WORDTOWER_API_KEY"]], separators=(",", ":")).encode()).hexdigest()


def write_state(path, state):
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            json.dump(state, target, ensure_ascii=False, indent=2)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def state_lock(path):
    lock = path.with_suffix(path.suffix + ".lock")
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise RuntimeError("Recovery file is locked; check the owning process before removing a stale lock") from None
    try:
        with os.fdopen(fd, "w") as target:
            target.write(str(os.getpid()))
        yield
    finally:
        lock.unlink(missing_ok=True)


def load_state(path, binding):
    if path.stat().st_size > 65536:
        raise RuntimeError("Invalid recovery file size")
    state = json.loads(path.read_text(encoding="utf-8"))
    if (state.get("format_version") != "eng-craft.wordtower-import.v1" or state.get("connection") != binding
            or not isinstance(state.get("submit"), dict) or state["submit"].get("api_name") not in {"/imports/text", "/imports/epub"}
            or set(state["submit"]) != {"api_name", "preview_id", "idempotency_key"}
            or not isinstance(state["submit"].get("preview_id"), str)
            or not isinstance(state["submit"].get("idempotency_key"), str)):
        raise RuntimeError("Recovery file belongs to another credential/destination or has invalid fields")
    return state


def continue_import(script_path, path, state, env, max_wait=120):
    if state.get("status") in {"failed", "cancelled"}:
        raise RuntimeError("Import is terminal; inspect it before explicitly starting a new import")
    if state.get("status") == "succeeded":
        print("Already succeeded; no new task submitted. Job ID: " + str(state.get("job_id")))
        return state
    if not state.get("job_id"):
        state["status"] = "submit_unknown"
        write_state(path, state)  # Persist the original key before any potentially successful write.
        response = run_request_json(script_path, state["submit"], env)
        job = (response.get("data") or {}).get("job_id")
        if not job:
            raise RuntimeError("Submission result is unclear; resume with the original recovery file")
        state.update(job_id=job, status="accepted")
        write_state(path, state)
    deadline = time.monotonic() + max_wait
    while True:
        response = run_request_json(script_path, {"api_name": "/imports/status", "job_id": state["job_id"]}, env)
        data = response.get("data") or {}
        status = data.get("status")
        state["status"] = status or "unknown"
        write_state(path, state)
        if status == "succeeded":
            print("✓ Course import succeeded. Job ID: " + str(state["job_id"]))
            for item in [data, *(data.get("chapters") or [])]:
                if isinstance(item, dict):
                    for key in ("course_id", "reader_link", "completed_chapters", "billed_chars"):
                        if item.get(key) is not None:
                            print(f"  {key}: {item[key]}")
            return state
        if status in {"failed", "cancelled"}:
            raise RuntimeError("WordTower import " + status + "; recovery file retained")
        delay = max(1, min(float(data.get("poll_after_seconds") or 4), 60))
        if time.monotonic() + delay >= deadline:
            print("Still processing; resume with: " + str(path))
            return state
        time.sleep(delay)


def submit_and_poll(script_path, preview_resp, submit_api, env, preview_only=False, max_wait=120,
                    identity=None, state_dir=None, new_import=False):
    data = preview_resp.get("data") or {}
    if not data.get("preview_id"):
        raise RuntimeError("WordTower did not return a preview ID")
    print("Preview: " + json.dumps({key: data.get(key) for key in (
        "title", "normalized_chars", "chapter_count", "subtitle_count", "word_timing_count", "quote", "quota")}, ensure_ascii=False))
    if preview_only:
        print(json.dumps(preview_resp, ensure_ascii=False, indent=2))
        return
    binding = connection_identity(script_path, env)
    directory = Path(state_dir or ".wordtower-imports").resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256((binding + (identity or data["preview_id"])).encode()).hexdigest()
    path = directory / (key + ("-" + uuid.uuid4().hex if new_import else "") + ".json")
    print("Recovery file: " + str(path))
    with state_lock(path):
        if path.exists():
            state = load_state(path, binding)
        else:
            state = {"format_version": "eng-craft.wordtower-import.v1", "connection": binding, "status": "prepared",
                     "submit": {"api_name": submit_api, "preview_id": data["preview_id"], "idempotency_key": str(uuid.uuid4())}}
            write_state(path, state)
        return continue_import(script_path, path, state, env, max_wait)


def resume_import(path, max_wait=120):
    script = get_required_request_script()
    env = prepare_env()
    path = Path(path).resolve()
    with state_lock(path):
        state = load_state(path, connection_identity(script, env))
        return continue_import(script, path, state, env, max_wait)


def export_material(kind, file, title=None, url=None, preview_only=False, prepare_only=False,
                    output=None, state_dir=None, new_import=False):
    path = Path(file).resolve()
    limit = 20 * 1024 * 1024 if kind == "epub" else 1024 * 1024
    if path.stat().st_size > limit:
        raise ValueError("Input exceeds the official import size limit")
    input_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    if kind == "text":
        content = path.read_text(encoding="utf-8-sig")
        if path.suffix.lower() in {".srt", ".vtt", ".json3"} or re.search(r"^\s*\d{2}:\d{2}.*-->", content, re.M):
            raise ValueError("Timed transcripts must use youtube URL --subtitles FILE; do not fall back to text")
    if kind == "youtube":
        url = canonical_youtube_url(url)
    title = title or path.stem
    script = get_required_request_script()
    arguments = ["--youtube", url, "--subtitles", str(path)] if kind == "youtube" else ["--" + kind, str(path)]
    arguments += ["--title", title]
    if kind == "youtube" and path.suffix.lower() == ".json3" and Path(script).is_file():
        if ".json3" not in Path(script).read_text(encoding="utf-8"):
            raise RuntimeError("Update the official WordTower Skill to preserve JSON3 word timings")
    if prepare_only:
        env = dict(os.environ)
        for key in ("WORDTOWER_API_KEY", "WORDS_API_KEY"):
            env.pop(key, None)
        payload = run_helper(script, arguments + ["--prepare-only"], env)
        if output:
            with Path(output).open("x", encoding="utf-8") as target:
                json.dump(payload, target, ensure_ascii=False, indent=2)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return payload
    env = prepare_env()
    check_capabilities(script, kind, env, preview_only)
    preview = run_helper(script, arguments, env)
    if kind == "youtube":
        validate_youtube_preview(preview, url)
    if hashlib.sha256(path.read_bytes()).hexdigest() != input_hash:
        raise RuntimeError("Input changed during preview; stop before submitting")
    identity = hashlib.sha256(json.dumps([kind, title, url, input_hash]).encode()).hexdigest()
    return submit_and_poll(script, preview, "/imports/epub" if kind == "epub" else "/imports/text", env,
                           preview_only=preview_only, identity=identity, state_dir=state_dir, new_import=new_import)


def export_epub(epub_path, title=None, preview_only=False, **options):
    return export_material("epub", epub_path, title, preview_only=preview_only, **options)


def export_text(text_path, title=None, preview_only=False, **options):
    return export_material("text", text_path, title, preview_only=preview_only, **options)


def export_youtube(youtube_url, subtitles_path, title=None, preview_only=False, **options):
    return export_material("youtube", subtitles_path, title, url=youtube_url, preview_only=preview_only, **options)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-script", type=Path, help="Official request.py from the currently loaded Skill")
    subs = parser.add_subparsers(dest="type", required=True)
    for kind in ("epub", "text", "youtube"):
        child = subs.add_parser(kind)
        child.add_argument("url" if kind == "youtube" else "file")
        if kind == "youtube":
            child.add_argument("--subtitles", "-s", required=True)
        child.add_argument("--title", "-t")
        mode = child.add_mutually_exclusive_group()
        mode.add_argument("--preview-only", action="store_true")
        mode.add_argument("--prepare-only", action="store_true", help="Offline MCP parameters; no Key or business requests")
        child.add_argument("--output", type=Path, help="Write prepared JSON to a new file")
        child.add_argument("--state-dir", type=Path)
        child.add_argument("--new-import", action="store_true", help="Explicitly create another import instead of reusing recovery")
    resume = subs.add_parser("resume")
    resume.add_argument("file", type=Path)
    resume.add_argument("--max-wait", type=float, default=120)
    args = parser.parse_args()
    if args.request_script:
        os.environ["WORDTOWER_REQUEST_SCRIPT"] = str(args.request_script)
    if args.type == "resume":
        resume_import(args.file, max_wait=args.max_wait)
    else:
        if args.output and not args.prepare_only:
            parser.error("--output requires --prepare-only")
        if args.prepare_only and (args.state_dir or args.new_import):
            parser.error("Offline preparation does not accept recovery options")
        options = dict(preview_only=args.preview_only, prepare_only=args.prepare_only, output=args.output,
                       state_dir=args.state_dir, new_import=args.new_import)
        export_material(args.type, args.subtitles if args.type == "youtube" else args.file,
                        args.title, url=args.url if args.type == "youtube" else None, **options)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        if isinstance(error, RuntimeError):
            print(str(error), file=sys.stderr)
        else:
            print("WordTower operation failed; check the input/environment and retain any recovery file", file=sys.stderr)
        sys.exit(1)
