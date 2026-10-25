#!/usr/bin/env python3
"""
sendlink.py - Sends the Sacrament Meeting YouTube livestream link
via Gmail (single BCC email) and/or individual text messages
using a Google Sheet distribution list configured in sendlink-preferences.json.
"""

import argparse
import csv
import datetime
import getpass
import http.server
import io
import json
import os
import pathlib
import re
import smtplib
import ssl
import subprocess
import sys
import urllib.parse
import urllib.request
import webbrowser
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from typing import Any, Dict, List, Optional

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 465
KEYCHAIN_SERVICE = "BroadcastDistroGmail"
DEFAULT_PREFS_FILE = "sendlink-preferences.json"
GOOGLE_OAUTH_SCOPES = (
    "https://www.googleapis.com/auth/youtube "
    "https://www.googleapis.com/auth/spreadsheets.readonly "
    "https://www.googleapis.com/auth/drive.readonly "
    "openid https://www.googleapis.com/auth/userinfo.email"
)
REQUIRED_SHEET_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"


def get_ssl_context() -> ssl.SSLContext:
    """Return an SSLContext using macOS system root certificates if available."""
    macos_cert = "/etc/ssl/cert.pem"
    if os.path.exists(macos_cert):
        return ssl.create_default_context(cafile=macos_cert)
    return ssl.create_default_context()


def load_preferences(prefs_path: pathlib.Path) -> Dict[str, Any]:
    """Load preferences from sendlink-preferences.json if present."""
    if not prefs_path.exists():
        return {}
    try:
        data = json.loads(prefs_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception as exc:
        print(
            f"Warning: Could not parse preferences file '{prefs_path}': {exc}",
            file=sys.stderr,
        )
    return {}


def get_pref_str(prefs: Dict[str, Any], key: str, default: str = "") -> str:
    """Retrieve a string value from prefs (case-insensitive key lookup)."""
    for k in (key, key.lower(), key.upper()):
        val = prefs.get(k)
        if val is not None and str(val).strip() != "":
            return str(val).strip()
    return default


def get_gmail_password(sender: str, prefs: Dict[str, Any]) -> str:
    """
    Retrieve the Gmail App Password from:
      1. sendlink-preferences.json ("gmail_app_password")
      2. macOS Keychain (service 'BroadcastDistroGmail', account sender)
      3. Interactive prompt
    """
    pwd = get_pref_str(prefs, "gmail_app_password")
    if pwd:
        return pwd.replace(" ", "")

    # Try macOS Keychain
    res = subprocess.run(
        [
            "security",
            "find-generic-password",
            "-a",
            sender,
            "-s",
            KEYCHAIN_SERVICE,
            "-w",
        ],
        capture_output=True,
        text=True,
    )
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout.strip().replace(" ", "")

    print(f"\nNo Gmail App Password found for {sender}.")
    print(
        f"Tip: You can set 'gmail_app_password' in {DEFAULT_PREFS_FILE} or save it to your macOS Keychain."
    )
    pwd = getpass.getpass(f"Enter Gmail App Password for {sender}: ").strip()
    if not pwd:
        raise RuntimeError("Gmail App Password is required to send email.")
    return pwd.replace(" ", "")


def get_access_token(
    client_secrets_path: pathlib.Path,
    token_path: pathlib.Path,
    account_hint: str = "moderator@telfordward.org",
) -> str:
    """Authenticate with Google OAuth 2.0 as account_hint (using cached token.json or browser login)."""
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

    # 1. Check existing token.json and ensure it includes Google Sheets read scope
    if token_path.exists():
        token_data = json.loads(token_path.read_text(encoding="utf-8"))
        granted_scopes = token_data.get("scope", "")
        if REQUIRED_SHEET_SCOPE in granted_scopes:
            access_token = token_data.get("access_token")
            refresh_token = token_data.get("refresh_token")
            expires_at = token_data.get("expires_at", 0)

            if (
                access_token
                and datetime.datetime.now().timestamp() < expires_at - 60
            ):
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
                    if "scope" in refreshed:
                        token_data["scope"] = refreshed["scope"]
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
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), OAuthCallbackHandler)
    port = server.server_port
    redirect_uri = f"http://127.0.0.1:{port}"

    auth_params_dict = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GOOGLE_OAUTH_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    }
    if account_hint:
        auth_params_dict["login_hint"] = account_hint

    auth_params = urllib.parse.urlencode(auth_params_dict)
    login_url = f"{auth_uri}?{auth_params}"
    print(
        f"\nOpening browser for Google OAuth authentication ({account_hint})..."
    )
    print(f"If your browser does not open automatically, visit:\n{login_url}\n")
    webbrowser.open(login_url)

    server.handle_request()
    server.server_close()

    if auth_code_holder["error"] or not auth_code_holder["code"]:
        raise RuntimeError(
            f"OAuth authorization failed: {auth_code_holder['error']}"
        )

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


def fetch_recipients(
    csv_url: str,
    access_token: str,
    recipient_filter: Optional[str] = None,
) -> List[Dict[str, str]]:
    """
    Fetch the Google Sheet as CSV using OAuth Bearer token.
    - If recipient_filter is None, return rows where Send == 'Y'.
    - If recipient_filter is provided, match by recipient name or phone number
      across all rows in the sheet (falling back to an ad-hoc phone recipient
      if a phone number is given that is not in the sheet).
    """
    req = urllib.request.Request(
        csv_url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X)",
        },
    )
    ctx = get_ssl_context()
    with urllib.request.urlopen(req, timeout=15, context=ctx) as response:
        content = response.read().decode("utf-8-sig")

    reader = csv.DictReader(io.StringIO(content))
    all_rows = []
    for row in reader:
        send_flag = (row.get("Send") or "").strip().upper()
        first_name = (row.get("First Name") or "").strip()
        last_name = (row.get("Last Name") or "").strip()
        full_name = f"{first_name} {last_name}".strip()
        email = (row.get("Email Address") or "").strip()
        phone = (
            row.get("Phone Digits") or row.get("Mobile phone") or ""
        ).strip()
        raw_service = (row.get("Service") or "").strip()
        service = "iMessage" if raw_service.lower() == "imessage" else "SMS"
        notes = (row.get("Notes") or "").strip()
        all_rows.append(
            {
                "send": send_flag,
                "first_name": first_name,
                "last_name": last_name,
                "full_name": full_name,
                "email": email,
                "phone": re.sub(r"\D", "", phone) if phone else "",
                "service": service,
                "notes": notes,
            }
        )

    if not recipient_filter:
        return [r for r in all_rows if r["send"] == "Y"]

    q = recipient_filter.strip()
    q_lower = q.lower()
    q_digits = re.sub(r"\D", "", q)
    q_phone_10 = q_digits[-10:] if len(q_digits) >= 10 else q_digits

    matched = []
    for r in all_rows:
        r_digits = re.sub(r"\D", "", r["phone"])
        r_phone_10 = r_digits[-10:] if len(r_digits) >= 10 else r_digits

        # Match by phone digits if query has at least 7 digits
        if len(q_digits) >= 7 and r_phone_10 and r_phone_10 == q_phone_10:
            matched.append(r)
            continue

        # Match by name (full name, first name, last name, or substring)
        if q_lower and (
            q_lower == r["full_name"].lower()
            or q_lower == r["first_name"].lower()
            or q_lower == r["last_name"].lower()
            or q_lower in r["full_name"].lower()
        ):
            matched.append(r)

    # If no sheet row matched, but the filter is a 10/11-digit phone number, allow ad-hoc send
    if not matched and len(q_digits) in (10, 11):
        matched.append(
            {
                "send": "Y",
                "first_name": "",
                "last_name": "",
                "full_name": q,
                "email": "",
                "phone": q_phone_10,
                "service": "SMS",
                "notes": "Ad-hoc phone recipient",
            }
        )

    return matched


def normalize_phone(phone: str) -> str:
    """Normalize a US phone number for Apple Messages (e.g. '8014133211' -> '+18014133211')."""
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 10:
        return f"+1{digits}"
    elif len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return digits or phone.strip()


def format_sender_header(sender_raw: str, sender_name: str = "") -> str:
    """Format the From header with an optional display name."""
    parsed_name, parsed_addr = parseaddr(sender_raw)
    addr = parsed_addr or sender_raw.strip()
    name = sender_name.strip() or parsed_name.strip()
    if name:
        return formataddr((name, addr))
    return addr


def send_email_via_gmail(
    sender_header: str,
    bcc_list: List[str],
    subject: str,
    body: str,
    prefs: Dict[str, Any],
) -> None:
    """Send a single email via Gmail SMTP with all recipients on BCC."""
    _, parsed_addr = parseaddr(sender_header)
    raw_sender_email = parsed_addr or sender_header.strip()
    smtp_user = get_pref_str(prefs, "gmail_user", raw_sender_email)
    app_password = get_gmail_password(smtp_user, prefs)

    msg = EmailMessage()
    msg["From"] = sender_header
    msg["Bcc"] = ", ".join(bcc_list)
    msg["Subject"] = subject
    msg.set_content(body)

    ctx = get_ssl_context()
    with smtplib.SMTP_SSL(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, context=ctx) as server:
        server.login(smtp_user, app_password)
        server.send_message(msg)


def send_text_via_messages(
    phone: str, message: str, service: str = "SMS"
) -> None:
    """Send an individual text message via macOS Messages app using iMessage or SMS."""
    normalized = normalize_phone(phone)
    service_arg = "iMessage" if service.lower() == "imessage" else "SMS"
    applescript = """
on run argv
    set targetPhone to item 1 of argv
    set textBody to item 2 of argv
    set serviceName to item 3 of argv
    tell application "Messages"
        if serviceName is "iMessage" then
            set targetService to 1st account whose service type = iMessage
        else
            set targetService to 1st account whose service type = SMS
        end if
        set theBuddy to participant targetPhone of targetService
        send textBody to theBuddy
    end tell
end run
"""
    cmd = ["osascript", "-e", applescript, normalized, message, service_arg]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"Messages {service_arg} failed for {phone} ({normalized}): {result.stderr.strip()}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send Sacrament Meeting livestream link via Gmail (BCC) and/or Text."
    )
    parser.add_argument(
        "link",
        nargs="?",
        help="Link to the YouTube livestream. If omitted, you will be prompted.",
    )
    parser.add_argument(
        "--preferences",
        "-p",
        default=DEFAULT_PREFS_FILE,
        help=f"Path to preferences JSON file (default: {DEFAULT_PREFS_FILE}).",
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Show proposed recipients and how they will receive the communication without sending.",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--text-only",
        action="store_true",
        help="Only send text messages (skip email).",
    )
    mode_group.add_argument(
        "--email-only",
        action="store_true",
        help="Only send email (skip text messages).",
    )
    parser.add_argument(
        "--recipient",
        "-r",
        default=None,
        help="Send only to a specific recipient matching this name or phone number.",
    )
    parser.add_argument(
        "--sheet-url",
        default=None,
        help="Optional override for Google Sheet CSV export URL.",
    )
    args = parser.parse_args()

    script_dir = pathlib.Path(__file__).resolve().parent
    prefs_path = pathlib.Path(args.preferences)
    if not prefs_path.is_absolute():
        prefs_path = script_dir / prefs_path
    prefs = load_preferences(prefs_path)

    sheet_id = get_pref_str(prefs, "sheet_id")
    sender_email = get_pref_str(prefs, "sender_email")
    sender_name = get_pref_str(prefs, "sender_name")
    sender_header = format_sender_header(sender_email, sender_name)
    _, raw_sender_email = parseaddr(sender_email)
    raw_sender_email = raw_sender_email or sender_email

    send_email = not args.text_only
    send_text = not args.email_only

    sheet_url = args.sheet_url or get_pref_str(prefs, "sheet_url")
    if not sheet_url:
        if not sheet_id:
            print(
                f"Error: 'sheet_id' is not set in {prefs_path.name} (and --sheet-url was not provided).",
                file=sys.stderr,
            )
            return 1
        sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"

    if send_email and not sender_email:
        print(
            f"Error: 'sender_email' is not set in {prefs_path.name}.",
            file=sys.stderr,
        )
        return 1

    livestream_link = (args.link or "").strip()
    if not livestream_link:
        try:
            livestream_link = input(
                "Enter the link to the YouTube livestream: "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.", file=sys.stderr)
            return 1

    if not livestream_link:
        print("Error: A livestream link is required.", file=sys.stderr)
        return 1

    today_mm_dd = datetime.date.today().strftime("%m/%d")
    email_subject = f"Link for Sacrament Meeting {today_mm_dd}"
    email_body = livestream_link
    text_body = f"Link for Sacrament Meeting today {livestream_link}"

    client_secrets_file = get_pref_str(
        prefs, "client_secrets_file", "client_secret.json"
    )
    token_file = get_pref_str(prefs, "token_file", "token.json")
    client_secrets_path = pathlib.Path(client_secrets_file)
    if not client_secrets_path.is_absolute():
        client_secrets_path = script_dir / client_secrets_path
    token_path = pathlib.Path(token_file)
    if not token_path.is_absolute():
        token_path = script_dir / token_path

    auth_account = raw_sender_email or "moderator@telfordward.org"

    print(
        f"Fetching recipient list from Google Sheet (authenticated as {auth_account})..."
    )
    try:
        access_token = get_access_token(
            client_secrets_path, token_path, account_hint=auth_account
        )
        recipients = fetch_recipients(
            sheet_url, access_token, recipient_filter=args.recipient
        )
    except Exception as exc:
        print(f"Error fetching Google Sheet: {exc}", file=sys.stderr)
        return 1

    if args.recipient and not recipients:
        print(
            f"No recipient found matching '{args.recipient}'. Nothing to send.",
            file=sys.stderr,
        )
        return 1

    email_recipients = [r for r in recipients if r["email"]] if send_email else []
    text_recipients = [r for r in recipients if r["phone"]] if send_text else []

    if send_email and send_text:
        mode_label = "Both Email and Text"
    elif send_email:
        mode_label = "Email Only (--email-only)"
    else:
        mode_label = "Text Only (--text-only)"

    if args.dry_run:
        print("\n" + "=" * 75)
        print("DRY RUN MODE - NO MESSAGES WILL BE SENT")
        print("=" * 75)
        print(f"Mode            : {mode_label}")
        if args.recipient:
            print(f"Recipient Filter: {args.recipient}")
        print(f"Livestream Link : {livestream_link}")
        print(f"Total Selected  : {len(recipients)}")
        print("-" * 75)
        print(f"{'Recipient':<22} | {'How They Receive It':<28} | {'Destination'}")
        print("-" * 75)
        for r in recipients:
            channels = []
            details = []
            if send_email and r["email"]:
                channels.append("Email (Gmail BCC)")
                details.append(r["email"])
            if send_text and r["phone"]:
                channels.append(f"Text ({r['service']})")
                details.append(r["phone"])
            if not channels:
                channels.append("Skipped (Mode/No info)")
            print(
                f"{r['full_name']:<22} | {', '.join(channels):<28} | {', '.join(details)}"
            )
        print("-" * 75)

        if send_email:
            print("\n[Proposed Gmail Broadcast (1 Email with BCC)]")
            print(f"  From    : {sender_header} (via Gmail)")
            print(f"  Subject : {email_subject}")
            print(
                f"  BCC ({len(email_recipients)}) : {',\n '.join(r['email'] for r in email_recipients) or 'None'}"
            )
            print(f"  Body    : {email_body}")
        else:
            print("\n[Proposed Gmail Broadcast] -> Skipped (--text-only)")

        if send_text:
            print("\n[Proposed Text Message Broadcast (Individual Texts)]")
            print(f"  Count   : {len(text_recipients)} individual message(s)")
            print(f"  Message : {text_body}")
            if text_recipients:
                for r in text_recipients:
                    print(
                        f"    -> {r['full_name']}:\t {r['phone']} ({normalize_phone(r['phone'])}) [{r['service']}]"
                    )
        else:
            print("\n[Proposed Text Message Broadcast] -> Skipped (--email-only)")

        print("=" * 75)
        return 0

    if not recipients:
        print("No recipients have Send set to 'Y'. Nothing to send.")
        return 0

    # 1. Send single BCC email via Gmail (if enabled)
    if send_email:
        if email_recipients:
            bcc_addresses = [r["email"] for r in email_recipients]
            print(
                f"\nSending 1 email via Gmail from {sender_header} with {len(bcc_addresses)} BCC recipient(s)..."
            )
            try:
                send_email_via_gmail(
                    sender_header,
                    bcc_addresses,
                    email_subject,
                    email_body,
                    prefs,
                )
                print(f"  ✓ Email sent to BCC: {', '.join(bcc_addresses)}")
            except Exception as exc:
                print(f"  ✗ Failed to send email: {exc}", file=sys.stderr)
        else:
            print("\nNo recipients with email addresses marked 'Y'.")

    # 2. Send individual text messages (if enabled)
    if send_text:
        if text_recipients:
            print(f"\nSending {len(text_recipients)} individual text message(s)...")
            for r in text_recipients:
                svc = r.get("service", "SMS")
                try:
                    send_text_via_messages(r["phone"], text_body, service=svc)
                    print(
                        f"  ✓ Text ({svc}) sent to {r['full_name']} ({r['phone']})"
                    )
                except Exception as exc:
                    print(
                        f"  ✗ Failed to text ({svc}) {r['full_name']} ({r['phone']}): {exc}",
                        file=sys.stderr,
                    )
        else:
            print("\nNo recipients with mobile phone numbers marked 'Y'.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
