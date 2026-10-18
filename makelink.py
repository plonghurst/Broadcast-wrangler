#!/usr/bin/env python3
"""
makelink.py - Automatically creates a YouTube Livestream broadcast and outputs
the shareable watch link (https://youtube.com/live/<VIDEO_ID>).

Order of operation for parameters:
  1. Read defaults from preferences file (makelink-preferences.json)
  2. Override with command-line arguments (if provided)
  3. Prompt interactively for any required parameter still missing
"""

import argparse
import datetime
import http.server
import json
import os
import pathlib
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube"
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
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
    Convert a time string like '10:00', '2026-10-18 10:00', or full ISO-8601
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


def main() -> int:
    script_dir = pathlib.Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(
        description="Create a YouTube Livestream and output its watch link."
    )
    parser.add_argument(
        "--prefs",
        default=str(script_dir / DEFAULT_PREFS_FILE),
        help="Path to preferences JSON file (default: makelink-preferences.json).",
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
        help="Scheduled start time (e.g. '10:00', '2026-10-18 10:00', or 'now'). Overrides preference.",
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

    # Step 2 & 3: Override with CLI args; if still missing, prompt interactively
    today_mm_dd = datetime.date.today().strftime("%m/%d")

    raw_title = resolve_param(
        args.title,
        prefs,
        "title",
        "Enter livestream title (use {date} for MM/DD)",
        default_if_empty=f"Sacrament Meeting {today_mm_dd}",
    )
    title = raw_title.replace("{date}", today_mm_dd)

    # Description can be empty without prompting if present in prefs as ""
    if args.description is not None:
        description = args.description
    elif "description" in prefs:
        description = str(prefs.get("description") or "").replace(
            "{date}", today_mm_dd
        )
    else:
        description = resolve_param(
            None,
            prefs,
            "description",
            "Enter livestream description (optional)",
            default_if_empty="",
        )

    category = resolve_param(
        args.category,
        prefs,
        "category",
        "Enter broadcast category",
        default_if_empty="Non Profit/Activism",
    )
    category_id = resolve_category_id(category)

    timezone_name = resolve_param(
        args.timezone,
        prefs,
        "timezone",
        "Enter timezone (e.g. America/Denver)",
        default_if_empty="America/Denver",
    )

    raw_start_time = resolve_param(
        args.start_time,
        prefs,
        "start_time",
        "Enter scheduled start time (HH:MM, YYYY-MM-DD HH:MM, or 'now')",
        default_if_empty="now",
    )
    scheduled_start_time = format_iso_start_time(raw_start_time, timezone_name)

    privacy = resolve_param(
        args.privacy,
        prefs,
        "privacy",
        "Enter privacy status (public, unlisted, private)",
        default_if_empty="unlisted",
    ).lower()

    unlist_replay = resolve_bool_param(
        args.unlist_replay,
        prefs,
        "unlist_replay",
        "Unlist replay once the stream ends?",
        default_bool=True,
    )

    disable_chat = resolve_bool_param(
        args.disable_chat,
        prefs,
        "disable_chat",
        "Disable live chat?",
        default_bool=True,
    )

    made_for_kids = resolve_bool_param(
        args.made_for_kids,
        prefs,
        "made_for_kids",
        "Is this stream Made for Kids?",
        default_bool=False,
    )

    latency = resolve_param(
        args.latency,
        prefs,
        "latency",
        "Enter stream latency (normal, low, ultraLow)",
        default_if_empty="normal",
    )

    enable_dvr = resolve_bool_param(
        args.enable_dvr,
        prefs,
        "enable_dvr",
        "Enable DVR (rewind during live)?",
        default_bool=True,
    )

    enable_auto_start = resolve_bool_param(
        args.enable_auto_start,
        prefs,
        "enable_auto_start",
        "Enable auto-start when encoder connects?",
        default_bool=True,
    )

    enable_auto_stop = resolve_bool_param(
        args.enable_auto_stop,
        prefs,
        "enable_auto_stop",
        "Enable auto-stop when encoder disconnects?",
        default_bool=True,
    )

    stream_id = (
        args.stream_id
        if args.stream_id is not None
        else str(prefs.get("stream_id") or "").strip()
    )

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
        "contentDetails": {
            "enableDvr": enable_dvr,
            "enableAutoStart": enable_auto_start,
            "enableAutoStop": enable_auto_stop,
            "latencyPreference": latency,
        },
    }

    if args.dry_run:
        print("=" * 70)
        print("DRY RUN MODE - YOUTUBE LIVESTREAM WILL NOT BE CREATED")
        print("=" * 70)
        print(f"Preferences File   : {prefs_path} ({'found' if prefs_path.exists() else 'not found'})")
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
        print(f"Bind Stream ID     : {stream_id or '(None / Default in YouTube Studio)'}")
        print("-" * 70)
        print("YouTube API Request Payload:")
        print(json.dumps(broadcast_body, indent=2))
        print("=" * 70)
        return 0

    try:
        access_token = get_access_token(client_secrets_path, token_path)
        created = youtube_api_request(
            "POST",
            "liveBroadcasts",
            access_token,
            {"part": "snippet,status,contentDetails"},
            body=broadcast_body,
        )
        broadcast_id = created["id"]

        # Set the video Category (e.g. '29' for Non Profit/Activism) on the created video
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

        watch_url = f"https://youtube.com/live/{broadcast_id}"
        print(watch_url)
        return 0
    except Exception as exc:
        print(f"Error creating YouTube livestream: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
