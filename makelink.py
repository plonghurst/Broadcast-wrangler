#!/usr/bin/env python3
"""
makelink.py - Automatically creates a YouTube Livestream broadcast and outputs
the shareable watch link (https://youtu.be/<VIDEO_ID>).

Supports reusing settings and thumbnail image from a previous livestream, plus
the 3-step order of operation for parameters:
  1. Read defaults from preferences file (makelink-preferences.json) / reused broadcast
  2. Override with command-line arguments (if provided)
  3. Prompt interactively for any required parameter still missing
"""

import argparse
import datetime
import http.server
import json
import os
import pathlib
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

YOUTUBE_UPLOAD_SCOPE = (
    "https://www.googleapis.com/auth/youtube "
    "https://www.googleapis.com/auth/spreadsheets.readonly "
    "https://www.googleapis.com/auth/drive.readonly "
    "openid https://www.googleapis.com/auth/userinfo.email"
)
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
YOUTUBE_UPLOAD_BASE = "https://www.googleapis.com/upload/youtube/v3"
DEFAULT_PREFS_FILE = "makelink-preferences.json"

CATEGORY_MAP = {
    "film & animation": "1",
    "autos & vehicles": "2",
    "music": "10",
    "pets & animals": "15",
    "sports": "17",
    "travel & events": "19",
    "gaming": "20",
    "people & blogs": "22",
    "comedy": "23",
    "entertainment": "24",
    "news & politics": "25",
    "howto & style": "26",
    "education": "27",
    "science & technology": "28",
    "nonprofits & activism": "29",
    "non profit/activism": "29",
    "non-profit/activism": "29",
    "nonprofit/activism": "29",
    "nonprofit": "29",
}


def resolve_category_id(category_str: str) -> str:
    """Map a category name or numeric ID to a YouTube categoryId (default '29' for Non Profit/Activism)."""
    s = category_str.strip()
    if s.isdigit():
        return s
    return CATEGORY_MAP.get(s.lower(), "29")


def extract_video_id(val: str) -> str:
    """Extract a YouTube video/broadcast ID from a URL or raw ID string."""
    s = val.strip()
    if not s or s.lower() == "latest":
        return s.lower()
    # Match youtube.com/live/<ID>, youtu.be/<ID>, or v=<ID>
    m = re.search(r"(?:youtube\.com/live/|youtu\.be/|[?&]v=)([A-Za-z0-9_-]{11})", s)
    if m:
        return m.group(1)
    return s


def get_ssl_context() -> ssl.SSLContext:
    """Return an SSLContext using macOS system root certificates if available."""
    macos_cert = "/etc/ssl/cert.pem"
    if os.path.exists(macos_cert):
        return ssl.create_default_context(cafile=macos_cert)
    return ssl.create_default_context()


def load_preferences(prefs_path: pathlib.Path) -> Dict[str, Any]:
    """Load preferences from JSON file if it exists."""
    if not prefs_path.exists():
        return {}
    try:
        return json.loads(prefs_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(
            f"Warning: Failed to parse preferences file {prefs_path}: {exc}",
            file=sys.stderr,
        )
        return {}


def parse_bool(val: Any) -> Optional[bool]:
    if isinstance(val, bool):
        return val
    if val is None:
        return None
    s = str(val).strip().lower()
    if s in ("true", "yes", "y", "1"):
        return True
    if s in ("false", "no", "n", "0"):
        return False
    return None


def resolve_param(
    cli_val: Any,
    prefs: Dict[str, Any],
    pref_key: str,
    prompt_text: str,
    default_if_empty: Optional[str] = None,
) -> str:
    """
    Resolve a string parameter using the 3-step order of operations:
      1. Preference value from preferences file
      2. Command-line argument overrides preference
      3. Interactive prompt if neither is present
    """
    val = prefs.get(pref_key)
    if cli_val is not None:
        val = cli_val

    if val is not None and str(val).strip() != "":
        return str(val).strip()

    # Neither preference nor CLI argument provided -> prompt user
    suffix = f" [{default_if_empty}]" if default_if_empty else ""
    while True:
        try:
            entered = input(f"{prompt_text}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.", file=sys.stderr)
            sys.exit(1)
        if entered:
            return entered
        if default_if_empty is not None:
            return default_if_empty
        print("This value is required. Please enter a value.")


def resolve_bool_param(
    cli_val: Optional[bool],
    prefs: Dict[str, Any],
    pref_key: str,
    prompt_text: str,
    default_bool: bool = False,
) -> bool:
    """Resolve a boolean parameter: prefs -> CLI override -> prompt."""
    val = parse_bool(prefs.get(pref_key))
    if cli_val is not None:
        val = cli_val
    if val is not None:
        return val

    default_hint = "y/N" if not default_bool else "Y/n"
    while True:
        try:
            entered = input(f"{prompt_text} ({default_hint}): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.", file=sys.stderr)
            sys.exit(1)
        if not entered:
            return default_bool
        parsed = parse_bool(entered)
        if parsed is not None:
            return parsed
        print("Please enter 'y' or 'n'.")


def format_iso_start_time(start_time_str: str, tz_name: str) -> str:
    """
    Convert a time string like '09:00', '2026-10-18 09:00', or full ISO-8601
    into a timezone-aware ISO-8601 timestamp required by YouTube.
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = datetime.datetime.now().astimezone().tzinfo

    now = datetime.datetime.now(tz)
    s = start_time_str.strip()

    if s.lower() == "now":
        dt = now + datetime.timedelta(minutes=1)
        return dt.isoformat(timespec="seconds")

    # Try HH:MM (24-hour) for today
    if len(s) <= 5 and ":" in s:
        parts = s.split(":")
        hour, minute = int(parts[0]), int(parts[1])
        dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if dt <= now:
            dt = now + datetime.timedelta(minutes=1)
        return dt.isoformat(timespec="seconds")

    # Try YYYY-MM-DD HH:MM
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            naive = datetime.datetime.strptime(s, fmt)
            dt = naive.replace(tzinfo=tz)
            return dt.isoformat(timespec="seconds")
        except ValueError:
            pass

    # Assume already ISO-8601
    return s


def get_access_token(
    client_secrets_path: pathlib.Path, token_path: pathlib.Path
) -> str:
    """Authenticate with Google OAuth 2.0 (using cached token.json or browser login)."""
    ctx = get_ssl_context()

    if not client_secrets_path.exists():
        raise FileNotFoundError(
            f"OAuth client secrets file not found at '{client_secrets_path}'.\n"
            "Download your OAuth 2.0 Desktop client JSON from Google Cloud Console "
            "and save it as 'client_secret.json'."
        )

    secrets_raw = json.loads(client_secrets_path.read_text(encoding="utf-8"))
    client_info = secrets_raw.get("installed") or secrets_raw.get("web") or {}
    client_id = client_info.get("client_id")
    client_secret = client_info.get("client_secret")
    token_uri = client_info.get(
        "token_uri", "https://oauth2.googleapis.com/token"
    )
    auth_uri = client_info.get(
        "auth_uri", "https://accounts.google.com/o/oauth2/v2/auth"
    )

    if not client_id or not client_secret:
        raise RuntimeError(
            f"Invalid client_secret.json format in {client_secrets_path}."
        )

    # 1. Check existing token.json
    if token_path.exists():
        token_data = json.loads(token_path.read_text(encoding="utf-8"))
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        expires_at = token_data.get("expires_at", 0)

        if access_token and datetime.datetime.now().timestamp() < expires_at - 60:
            return access_token

        if refresh_token:
            data = urllib.parse.urlencode(
                {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                }
            ).encode("utf-8")
            req = urllib.request.Request(token_uri, data=data, method="POST")
            try:
                with urllib.request.urlopen(req, context=ctx) as resp:
                    refreshed = json.loads(resp.read().decode("utf-8"))
                token_data["access_token"] = refreshed["access_token"]
                token_data["expires_at"] = (
                    datetime.datetime.now().timestamp()
                    + int(refreshed.get("expires_in", 3600))
                )
                token_path.write_text(
                    json.dumps(token_data, indent=2), encoding="utf-8"
                )
                return token_data["access_token"]
            except Exception:
                pass  # Fall back to interactive login if refresh fails

    # 2. Interactive OAuth 2.0 loopback flow
    auth_code_holder: Dict[str, Optional[str]] = {"code": None, "error": None}

    class OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if "code" in params:
                auth_code_holder["code"] = params["code"][0]
                body = b"<h2>Authentication complete! You may close this browser tab and return to the terminal.</h2>"
            else:
                auth_code_holder["error"] = params.get(
                    "error", ["Unknown error"]
                )[0]
                body = b"<h2>Authentication failed. Please check the terminal.</h2>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass  # Silence HTTP server logs

    server = http.server.HTTPServer(("127.0.0.1", 0), OAuthCallbackHandler)
    port = server.server_port
    redirect_uri = f"http://127.0.0.1:{port}"

    auth_params = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": YOUTUBE_UPLOAD_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    login_url = f"{auth_uri}?{auth_params}"
    print("\nOpening browser for YouTube OAuth authentication...")
    print(f"If your browser does not open automatically, visit:\n{login_url}\n")
    webbrowser.open(login_url)

    server.handle_request()
    server.server_close()

    if auth_code_holder["error"] or not auth_code_holder["code"]:
        raise RuntimeError(
            f"OAuth authorization failed: {auth_code_holder['error']}"
        )

    # Exchange authorization code for tokens
    token_req_data = urllib.parse.urlencode(
        {
            "code": auth_code_holder["code"],
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")
    req = urllib.request.Request(token_uri, data=token_req_data, method="POST")
    with urllib.request.urlopen(req, context=ctx) as resp:
        token_resp = json.loads(resp.read().decode("utf-8"))

    token_resp["expires_at"] = (
        datetime.datetime.now().timestamp()
        + int(token_resp.get("expires_in", 3600))
    )
    token_path.write_text(json.dumps(token_resp, indent=2), encoding="utf-8")
    return token_resp["access_token"]


def youtube_api_request(
    method: str,
    endpoint: str,
    access_token: str,
    params: Dict[str, str],
    body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Make an authenticated request to the YouTube Data API v3."""
    query = urllib.parse.urlencode(params)
    url = f"{YOUTUBE_API_BASE}/{endpoint}?{query}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    ctx = get_ssl_context()
    try:
        with urllib.request.urlopen(req, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"YouTube API HTTP {exc.code}: {err_body}"
        ) from exc


def fetch_template_broadcast(
    access_token: str, reuse_ref: str
) -> Optional[Dict[str, Any]]:
    """
    Fetch a previous YouTube broadcast to reuse its settings and thumbnail.
    reuse_ref can be a broadcast ID, a YouTube URL, or 'latest'.
    """
    vid = extract_video_id(reuse_ref)
    if not vid:
        return None

    if vid == "latest":
        resp = youtube_api_request(
            "GET",
            "liveBroadcasts",
            access_token,
            {
                "part": "id,snippet,status,contentDetails",
                "mine": "true",
                "maxResults": "10",
            },
        )
        items = resp.get("items", [])
        if not items:
            return None
        # Prefer the most recent broadcast that has a custom/maxres thumbnail, or the first one
        for item in items:
            if "maxres" in item.get("snippet", {}).get("thumbnails", {}):
                return item
        return items[0]

    resp = youtube_api_request(
        "GET",
        "liveBroadcasts",
        access_token,
        {"part": "id,snippet,status,contentDetails", "id": vid},
    )
    items = resp.get("items", [])
    return items[0] if items else None


def get_best_thumbnail_url(broadcast_item: Dict[str, Any]) -> Optional[str]:
    """Return the highest-resolution thumbnail URL from a broadcast item."""
    thumbs = broadcast_item.get("snippet", {}).get("thumbnails", {})
    for res in ("maxres", "standard", "high", "medium", "default"):
        url = thumbs.get(res, {}).get("url")
        if url:
            return url
    return None


def load_thumbnail_bytes(
    script_dir: pathlib.Path,
    thumbnail_arg: Optional[str],
    prefs: Dict[str, Any],
    template_broadcast: Optional[Dict[str, Any]],
) -> Tuple[Optional[bytes], str]:
    """
    Resolve thumbnail image bytes and source description.
    Order:
      1. --thumbnail CLI argument (local file path or URL)
      2. 'thumbnail_file' in preferences (if file exists)
      3. High-res thumbnail downloaded from reused broadcast on YouTube
    """
    ctx = get_ssl_context()
    source_candidate = (
        thumbnail_arg
        if thumbnail_arg is not None
        else str(prefs.get("thumbnail_file") or "").strip()
    )

    if source_candidate:
        if source_candidate.startswith(("http://", "https://")):
            req = urllib.request.Request(source_candidate)
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                return resp.read(), f"URL ({source_candidate})"
        path = (
            pathlib.Path(source_candidate)
            if os.path.isabs(source_candidate)
            else script_dir / source_candidate
        )
        if path.exists():
            return path.read_bytes(), f"Local file ({path})"

    if template_broadcast:
        thumb_url = get_best_thumbnail_url(template_broadcast)
        if thumb_url:
            req = urllib.request.Request(thumb_url)
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                data = resp.read()
            # Cache locally to thumbnail.jpg if thumbnail_file is configured
            if source_candidate and not source_candidate.startswith("http"):
                cache_path = (
                    pathlib.Path(source_candidate)
                    if os.path.isabs(source_candidate)
                    else script_dir / source_candidate
                )
                try:
                    cache_path.write_bytes(data)
                except Exception:
                    pass
            return data, f"Reused broadcast {template_broadcast['id']} ({thumb_url})"

    return None, "None"


def upload_thumbnail(
    access_token: str, video_id: str, image_bytes: bytes
) -> None:
    """Upload a custom thumbnail image to a YouTube broadcast/video via thumbnails.set."""
    query = urllib.parse.urlencode(
        {"videoId": video_id, "uploadType": "media"}
    )
    url = f"{YOUTUBE_UPLOAD_BASE}/thumbnails/set?{query}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "image/jpeg",
        "Content-Length": str(len(image_bytes)),
        "Accept": "application/json",
    }
    req = urllib.request.Request(
        url, data=image_bytes, headers=headers, method="POST"
    )
    ctx = get_ssl_context()
    try:
        with urllib.request.urlopen(req, context=ctx) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"YouTube Thumbnail Upload HTTP {exc.code}: {err_body}"
        ) from exc


def main() -> int:
    script_dir = pathlib.Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(
        description="Create a YouTube Livestream (reusing settings/thumbnail from a previous stream) and output its watch link."
    )
    parser.add_argument(
        "--prefs",
        default=str(script_dir / DEFAULT_PREFS_FILE),
        help="Path to preferences JSON file (default: makelink-preferences.json).",
    )
    parser.add_argument(
        "--reuse-from",
        help="Previous YouTube broadcast ID, URL, or 'latest' to reuse settings and thumbnail from. Overrides preference.",
    )
    parser.add_argument(
        "--thumbnail",
        help="Path or URL to custom thumbnail image to apply to the broadcast. Overrides preference.",
    )
    parser.add_argument(
        "--title",
        help="Broadcast title (supports {date} placeholder for MM/DD). Overrides preference.",
    )
    parser.add_argument(
        "--description",
        help="Broadcast description. Overrides preference.",
    )
    parser.add_argument(
        "--category",
        help="Broadcast category (e.g. 'Non Profit/Activism'). Overrides preference.",
    )
    parser.add_argument(
        "--start-time",
        help="Scheduled start time (e.g. '09:00', '2026-10-18 09:00', or 'now'). Overrides preference.",
    )
    parser.add_argument(
        "--timezone",
        help="IANA timezone name (e.g. 'America/Denver'). Overrides preference.",
    )
    parser.add_argument(
        "--privacy",
        choices=["public", "unlisted", "private"],
        help="Privacy status: public, unlisted, or private. Overrides preference.",
    )
    parser.add_argument(
        "--unlist-replay",
        dest="unlist_replay",
        action="store_true",
        default=None,
        help="Unlist the replay once the stream ends.",
    )
    parser.add_argument(
        "--no-unlist-replay",
        dest="unlist_replay",
        action="store_false",
        default=None,
        help="Do not unlist the replay once the stream ends.",
    )
    parser.add_argument(
        "--disable-chat",
        dest="disable_chat",
        action="store_true",
        default=None,
        help="Disable live chat on the broadcast.",
    )
    parser.add_argument(
        "--enable-chat",
        dest="disable_chat",
        action="store_false",
        default=None,
        help="Enable live chat on the broadcast.",
    )
    parser.add_argument(
        "--made-for-kids",
        dest="made_for_kids",
        action="store_true",
        default=None,
        help="Mark broadcast as Made for Kids.",
    )
    parser.add_argument(
        "--not-made-for-kids",
        dest="made_for_kids",
        action="store_false",
        default=None,
        help="Mark broadcast as NOT Made for Kids.",
    )
    parser.add_argument(
        "--latency",
        choices=["normal", "low", "ultraLow"],
        help="Stream latency preference (normal, low, ultraLow). Overrides preference.",
    )
    parser.add_argument(
        "--enable-auto-start",
        dest="enable_auto_start",
        action="store_true",
        default=None,
        help="Automatically start broadcast when encoder starts streaming.",
    )
    parser.add_argument(
        "--no-auto-start",
        dest="enable_auto_start",
        action="store_false",
        default=None,
        help="Disable auto-start.",
    )
    parser.add_argument(
        "--enable-auto-stop",
        dest="enable_auto_stop",
        action="store_true",
        default=None,
        help="Automatically stop broadcast when encoder stops streaming.",
    )
    parser.add_argument(
        "--no-auto-stop",
        dest="enable_auto_stop",
        action="store_false",
        default=None,
        help="Disable auto-stop.",
    )
    parser.add_argument(
        "--enable-dvr",
        dest="enable_dvr",
        action="store_true",
        default=None,
        help="Allow viewers to rewind during the livestream.",
    )
    parser.add_argument(
        "--no-dvr",
        dest="enable_dvr",
        action="store_false",
        default=None,
        help="Disable DVR.",
    )
    parser.add_argument(
        "--stream-id",
        help="Optional existing YouTube stream ID (stream key) to bind to the broadcast.",
    )
    parser.add_argument(
        "--list-streams",
        action="store_true",
        help="List existing YouTube stream keys/IDs on your channel and exit.",
    )
    parser.add_argument(
        "--list-broadcasts",
        action="store_true",
        help="List recent YouTube broadcasts on your channel and exit.",
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Show resolved broadcast settings without creating the livestream on YouTube.",
    )
    args = parser.parse_args()

    # Step 1: Read preferences from preferences file
    prefs_path = pathlib.Path(args.prefs)
    prefs = load_preferences(prefs_path)

    client_secrets_file = prefs.get("client_secrets_file", "client_secret.json")
    token_file = prefs.get("token_file", "token.json")
    client_secrets_path = (
        pathlib.Path(client_secrets_file)
        if os.path.isabs(client_secrets_file)
        else script_dir / client_secrets_file
    )
    token_path = (
        pathlib.Path(token_file)
        if os.path.isabs(token_file)
        else script_dir / token_file
    )

    if args.list_streams:
        token = get_access_token(client_secrets_path, token_path)
        resp = youtube_api_request(
            "GET",
            "liveStreams",
            token,
            {"part": "id,snippet,cdn", "mine": "true"},
        )
        items = resp.get("items", [])
        print(f"Found {len(items)} stream key(s) on your channel:")
        for item in items:
            sid = item.get("id")
            stitle = item.get("snippet", {}).get("title")
            sname = item.get("cdn", {}).get("ingestionInfo", {}).get("streamName")
            print(f"  - ID: {sid} | Title: {stitle} | Key: {sname}")
        return 0

    if args.list_broadcasts:
        token = get_access_token(client_secrets_path, token_path)
        resp = youtube_api_request(
            "GET",
            "liveBroadcasts",
            token,
            {
                "part": "id,snippet,status,contentDetails",
                "mine": "true",
                "maxResults": "10",
            },
        )
        items = resp.get("items", [])
        print(f"Found {len(items)} recent broadcast(s) on your channel:")
        for item in items:
            bid = item.get("id")
            st = item.get("status", {}).get("lifeCycleStatus", "")
            btitle = item.get("snippet", {}).get("title", "")
            has_thumb = "maxres" in item.get("snippet", {}).get("thumbnails", {})
            print(
                f"  - ID: {bid} | Status: {st:<9} | Custom Thumb: {str(has_thumb):<5} | Title: {btitle}"
            )
        return 0

    # Check if we are reusing a previous broadcast
    reuse_ref = (
        args.reuse_from
        if args.reuse_from is not None
        else str(prefs.get("reuse_broadcast_id") or "").strip()
    )
    template_broadcast: Optional[Dict[str, Any]] = None
    access_token: Optional[str] = None

    if reuse_ref:
        try:
            access_token = get_access_token(client_secrets_path, token_path)
            template_broadcast = fetch_template_broadcast(access_token, reuse_ref)
        except Exception as exc:
            print(
                f"Warning: Could not fetch template broadcast '{reuse_ref}': {exc}",
                file=sys.stderr,
            )

    # Seed defaults from the reused broadcast (if present), then overlay makelink-preferences.json
    effective_prefs: Dict[str, Any] = {}
    if template_broadcast:
        t_snippet = template_broadcast.get("snippet", {})
        t_status = template_broadcast.get("status", {})
        t_content = template_broadcast.get("contentDetails", {})
        effective_prefs.update(
            {
                "description": t_snippet.get("description", ""),
                "category": str(t_snippet.get("categoryId", "29")),
                "privacy": t_status.get("privacyStatus", "unlisted"),
                "made_for_kids": t_status.get(
                    "selfDeclaredMadeForKids",
                    t_status.get("madeForKids", False),
                ),
                "latency": t_content.get("latencyPreference", "normal"),
                "enable_dvr": t_content.get("enableDvr", True),
                "enable_auto_start": t_content.get("enableAutoStart", False),
                "enable_auto_stop": t_content.get("enableAutoStop", True),
                "stream_id": t_content.get("boundStreamId", ""),
            }
        )
    effective_prefs.update(prefs)

    # Step 2 & 3: Override with CLI args; if still missing, prompt interactively
    today_mm_dd = datetime.date.today().strftime("%m/%d")

    raw_title = resolve_param(
        args.title,
        effective_prefs,
        "title",
        "Enter livestream title (use {date} for MM/DD)",
        default_if_empty=f"Sacrament Meeting {today_mm_dd}",
    )
    title = raw_title.replace("{date}", today_mm_dd)

    if args.description is not None:
        description = args.description
    elif "description" in effective_prefs:
        description = str(effective_prefs.get("description") or "").replace(
            "{date}", today_mm_dd
        )
    else:
        description = resolve_param(
            None,
            effective_prefs,
            "description",
            "Enter livestream description (optional)",
            default_if_empty="",
        )

    category = resolve_param(
        args.category,
        effective_prefs,
        "category",
        "Enter broadcast category",
        default_if_empty="Non Profit/Activism",
    )
    category_id = resolve_category_id(category)

    timezone_name = resolve_param(
        args.timezone,
        effective_prefs,
        "timezone",
        "Enter timezone (e.g. America/Denver)",
        default_if_empty="America/Denver",
    )

    raw_start_time = resolve_param(
        args.start_time,
        effective_prefs,
        "start_time",
        "Enter scheduled start time (HH:MM, YYYY-MM-DD HH:MM, or 'now')",
        default_if_empty="now",
    )
    scheduled_start_time = format_iso_start_time(raw_start_time, timezone_name)

    privacy = resolve_param(
        args.privacy,
        effective_prefs,
        "privacy",
        "Enter privacy status (public, unlisted, private)",
        default_if_empty="unlisted",
    ).lower()

    unlist_replay = resolve_bool_param(
        args.unlist_replay,
        effective_prefs,
        "unlist_replay",
        "Unlist replay once the stream ends?",
        default_bool=True,
    )

    disable_chat = resolve_bool_param(
        args.disable_chat,
        effective_prefs,
        "disable_chat",
        "Disable live chat?",
        default_bool=True,
    )

    made_for_kids = resolve_bool_param(
        args.made_for_kids,
        effective_prefs,
        "made_for_kids",
        "Is this stream Made for Kids?",
        default_bool=False,
    )

    latency = resolve_param(
        args.latency,
        effective_prefs,
        "latency",
        "Enter stream latency (normal, low, ultraLow)",
        default_if_empty="normal",
    )

    enable_dvr = resolve_bool_param(
        args.enable_dvr,
        effective_prefs,
        "enable_dvr",
        "Enable DVR (rewind during live)?",
        default_bool=True,
    )

    enable_auto_start = resolve_bool_param(
        args.enable_auto_start,
        effective_prefs,
        "enable_auto_start",
        "Enable auto-start when encoder connects?",
        default_bool=False,
    )

    enable_auto_stop = resolve_bool_param(
        args.enable_auto_stop,
        effective_prefs,
        "enable_auto_stop",
        "Enable auto-stop when encoder disconnects?",
        default_bool=True,
    )

    stream_id = (
        args.stream_id
        if args.stream_id is not None
        else str(effective_prefs.get("stream_id") or "").strip()
    )

    # Preserve additional contentDetails from the template broadcast if available
    content_details: Dict[str, Any] = {
        "enableDvr": enable_dvr,
        "enableAutoStart": enable_auto_start,
        "enableAutoStop": enable_auto_stop,
        "latencyPreference": latency,
    }
    if template_broadcast:
        t_cd = template_broadcast.get("contentDetails", {})
        for key in (
            "enableEmbed",
            "recordFromStart",
            "enableClosedCaptions",
            "closedCaptionsType",
            "projection",
            "enableContentEncryption",
        ):
            if key in t_cd:
                content_details[key] = t_cd[key]
        if "monitorStream" in t_cd and "enableMonitorStream" in t_cd["monitorStream"]:
            content_details["monitorStream"] = {
                "enableMonitorStream": t_cd["monitorStream"]["enableMonitorStream"],
                "broadcastStreamDelayMs": t_cd["monitorStream"].get(
                    "broadcastStreamDelayMs", 0
                ),
            }

    broadcast_body = {
        "snippet": {
            "title": title,
            "description": description,
            "scheduledStartTime": scheduled_start_time,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": made_for_kids,
        },
        "contentDetails": content_details,
    }

    thumb_bytes, thumb_source = load_thumbnail_bytes(
        script_dir, args.thumbnail, effective_prefs, template_broadcast
    )

    if args.dry_run:
        print("=" * 72)
        print("DRY RUN MODE - YOUTUBE LIVESTREAM WILL NOT BE CREATED")
        print("=" * 72)
        print(
            f"Preferences File   : {prefs_path} ({'found' if prefs_path.exists() else 'not found'})"
        )
        if template_broadcast:
            t_title = template_broadcast.get("snippet", {}).get("title", "")
            print(
                f"Reused Broadcast   : {template_broadcast['id']} ({t_title})"
            )
        else:
            print(f"Reused Broadcast   : {reuse_ref or 'None'}")
        print(
            f"Thumbnail Image    : {thumb_source} ({len(thumb_bytes) if thumb_bytes else 0} bytes)"
        )
        print(f"Title              : {title}")
        print(f"Description        : {description}")
        print(f"Category           : {category} (YouTube categoryId={category_id})")
        print(f"Scheduled Start    : {scheduled_start_time} ({timezone_name})")
        print(f"Privacy Status     : {privacy}")
        print(f"Unlist Replay      : {unlist_replay}")
        print(f"Disable Live Chat  : {disable_chat}")
        print(f"Made for Kids      : {made_for_kids}")
        print(f"Latency Preference : {latency}")
        print(f"Enable DVR         : {enable_dvr}")
        print(f"Enable Auto-Start  : {enable_auto_start}")
        print(f"Enable Auto-Stop   : {enable_auto_stop}")
        print(
            f"Bind Stream ID     : {stream_id or '(None / Default in YouTube Studio)'}"
        )
        print("-" * 72)
        print("YouTube API Request Payload:")
        print(json.dumps(broadcast_body, indent=2))
        print("=" * 72)
        return 0

    try:
        if not access_token:
            access_token = get_access_token(client_secrets_path, token_path)

        created = youtube_api_request(
            "POST",
            "liveBroadcasts",
            access_token,
            {"part": "snippet,status,contentDetails"},
            body=broadcast_body,
        )
        broadcast_id = created["id"]

        # 1. Set the video Category (e.g. '29' for Non Profit/Activism) on the created video
        youtube_api_request(
            "PUT",
            "videos",
            access_token,
            {"part": "snippet"},
            body={
                "id": broadcast_id,
                "snippet": {
                    "title": title,
                    "description": description,
                    "categoryId": category_id,
                },
            },
        )

        # 2. Upload custom thumbnail image (from local file or reused broadcast)
        if thumb_bytes:
            upload_thumbnail(access_token, broadcast_id, thumb_bytes)

        # 3. Bind to existing stream key
        if stream_id:
            youtube_api_request(
                "POST",
                "liveBroadcasts/bind",
                access_token,
                {
                    "id": broadcast_id,
                    "part": "id,contentDetails",
                    "streamId": stream_id,
                },
            )

        watch_url = f"https://youtu.be/{broadcast_id}"
        print(watch_url)
        return 0
    except Exception as exc:
        print(f"Error creating YouTube livestream: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
