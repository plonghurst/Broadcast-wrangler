#!/usr/bin/env python3
"""
sendlink.py - Sends the Sacrament Meeting YouTube livestream link
via Gmail (single BCC email) and/or individual SMS text messages
using a Google Sheet distribution list configured in .env.
"""

import argparse
import csv
import datetime
import getpass
import io
import os
import pathlib
import re
import smtplib
import ssl
import subprocess
import sys
import urllib.request
from email.message import EmailMessage
from typing import Dict, List

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 465
KEYCHAIN_SERVICE = "BroadcastDistroGmail"


def get_ssl_context() -> ssl.SSLContext:
    """Return an SSLContext using macOS system root certificates if available."""
    macos_cert = "/etc/ssl/cert.pem"
    if os.path.exists(macos_cert):
        return ssl.create_default_context(cafile=macos_cert)
    return ssl.create_default_context()


def load_dotenv() -> None:
    """Load simple KEY=VALUE pairs from .env in the script directory if present."""
    env_path = pathlib.Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def get_gmail_password(sender: str) -> str:
    """
    Retrieve the Gmail App Password from:
      1. GMAIL_APP_PASSWORD environment variable (or .env file)
      2. macOS Keychain (service 'BroadcastDistroGmail', account sender)
      3. Interactive prompt
    """
    load_dotenv()
    pwd = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
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
        "Tip: You can set GMAIL_APP_PASSWORD in a .env file or save it to your macOS Keychain."
    )
    pwd = getpass.getpass(f"Enter Gmail App Password for {sender}: ").strip()
    if not pwd:
        raise RuntimeError("Gmail App Password is required to send email.")
    return pwd.replace(" ", "")


def fetch_recipients(csv_url: str) -> List[Dict[str, str]]:
    """Fetch the Google Sheet as CSV and return rows where Send == 'Y'."""
    req = urllib.request.Request(
        csv_url,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X)"},
    )
    ctx = get_ssl_context()
    with urllib.request.urlopen(req, timeout=15, context=ctx) as response:
        content = response.read().decode("utf-8-sig")

    reader = csv.DictReader(io.StringIO(content))
    selected = []
    for row in reader:
        send_flag = (row.get("Send") or "").strip().upper()
        if send_flag == "Y":
            first_name = (row.get("First Name") or "").strip()
            last_name = (row.get("Last Name") or "").strip()
            email = (row.get("Email Address") or "").strip()
            phone = (row.get("Phone Digits") or "").strip()
            notes = (row.get("Notes") or "").strip()
            selected.append(
                {
                    "first_name": first_name,
                    "last_name": last_name,
                    "full_name": f"{first_name} {last_name}".strip(),
                    "email": email,
                    "phone": phone,
                    "notes": notes,
                }
            )
    return selected


def normalize_phone(phone: str) -> str:
    """Normalize a US phone number for Apple Messages (e.g. '8014133211' -> '+18014133211')."""
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 10:
        return f"+1{digits}"
    elif len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return digits or phone.strip()


def send_email_via_gmail(
    sender: str, bcc_list: List[str], subject: str, body: str
) -> None:
    """Send a single email via Gmail SMTP with all recipients on BCC."""
    load_dotenv()
    smtp_user = os.environ.get("GMAIL_USER", sender).strip()
    app_password = get_gmail_password(smtp_user)

    msg = EmailMessage()
    msg["From"] = sender
    msg["Bcc"] = ", ".join(bcc_list)
    msg["Subject"] = subject
    msg.set_content(body)

    ctx = get_ssl_context()
    with smtplib.SMTP_SSL(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, context=ctx) as server:
        server.login(smtp_user, app_password)
        server.send_message(msg)


def send_text_via_messages(phone: str, message: str) -> None:
    """Send an individual SMS text message via macOS Messages app (Text Message Forwarding)."""
    normalized = normalize_phone(phone)
    applescript = """
on run argv
    set targetPhone to item 1 of argv
    set textBody to item 2 of argv
    tell application "Messages"
        set targetService to 1st account whose service type = SMS
        set theBuddy to participant targetPhone of targetService
        send textBody to theBuddy
    end tell
end run
"""
    cmd = ["osascript", "-e", applescript, normalized, message]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"Messages SMS failed for {phone} ({normalized}): {result.stderr.strip()}"
        )


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Send Sacrament Meeting livestream link via Gmail (BCC) and/or Text."
    )
    parser.add_argument(
        "link",
        nargs="?",
        help="Link to the YouTube livestream. If omitted, you will be prompted.",
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
        "--sheet-url",
        default=None,
        help="Optional override for Google Sheet CSV export URL.",
    )
    args = parser.parse_args()

    sheet_id = os.environ.get("SHEET_ID", "").strip()
    sender_email = os.environ.get("SENDER_EMAIL", "").strip()

    send_email = not args.text_only
    send_text = not args.email_only

    sheet_url = args.sheet_url
    if not sheet_url:
        if not sheet_id:
            print(
                "Error: SHEET_ID is not set in .env (and --sheet-url was not provided).",
                file=sys.stderr,
            )
            return 1
        sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"

    if send_email and not sender_email:
        print("Error: SENDER_EMAIL is not set in .env.", file=sys.stderr)
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

    print("Fetching recipient list from Google Sheet...")
    try:
        recipients = fetch_recipients(sheet_url)
    except Exception as exc:
        print(f"Error fetching Google Sheet: {exc}", file=sys.stderr)
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
        print(f"Livestream Link : {livestream_link}")
        print(f"Total Marked 'Y': {len(recipients)}")
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
                channels.append("Text (SMS)")
                details.append(r["phone"])
            if not channels:
                channels.append("Skipped (Mode/No info)")
            print(
                f"{r['full_name']:<22} | {', '.join(channels):<28} | {', '.join(details)}"
            )
        print("-" * 75)

        if send_email:
            print("\n[Proposed Gmail Broadcast (1 Email with BCC)]")
            print(f"  From    : {sender_email} (via Gmail)")
            print(f"  Subject : {email_subject}")
            print(
                f"  BCC ({len(email_recipients)}) : {', '.join(r['email'] for r in email_recipients) or 'None'}"
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
                        f"    -> {r['full_name']}: {r['phone']} ({normalize_phone(r['phone'])})"
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
                f"\nSending 1 email via Gmail from {sender_email} with {len(bcc_addresses)} BCC recipient(s)..."
            )
            try:
                send_email_via_gmail(
                    sender_email, bcc_addresses, email_subject, email_body
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
                try:
                    send_text_via_messages(r["phone"], text_body)
                    print(f"  ✓ Text sent to {r['full_name']} ({r['phone']})")
                except Exception as exc:
                    print(
                        f"  ✗ Failed to text {r['full_name']} ({r['phone']}): {exc}",
                        file=sys.stderr,
                    )
        else:
            print("\nNo recipients with mobile phone numbers marked 'Y'.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
