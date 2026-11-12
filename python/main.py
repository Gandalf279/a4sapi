import argparse
import datetime as dt
import json
import os
import sys
from cryptography.fernet import Fernet

import requests
import urllib3

from getpass import getpass

from lib import *
import threading
t = threading.Thread(target=lib_main)
t.start()

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
BASE_URL = "https://schule.louisenlund.de/api"
AUTH_URL = f"{BASE_URL}//Api/Authorization/JwtAuth"

USERNAME = input("Enter username > ")
PIN = getpass("Enter pin > ")

SCHOOL_ID = 35
SEMESTER_ID = 330
TEMP_FOLDER = "./temp"
DATA_FOLDER = "./data"
REQUEST_TIMEOUT = 15  # seconds

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)




from pathlib import Path

PASSWORD_FILE = Path("pw.txt")
KEY_FILE = Path("key.key")


def load_password() -> str:
    """Load or securely create an encrypted password."""

    if PASSWORD_FILE.exists() and KEY_FILE.exists():
        key = KEY_FILE.read_bytes()
        fernet = Fernet(key)

        try:
            encrypted_password = PASSWORD_FILE.read_bytes()
            return fernet.decrypt(encrypted_password).decode("utf-8")
        except Exception as exc:
            raise ScheduleError(
                "Could not decrypt password. Check your encryption key."
            ) from exc

    print("No saved password found.")
    password = getpass("Enter your password > ")

    key = Fernet.generate_key()
    fernet = Fernet(key)

    encrypted_password = fernet.encrypt(password.encode("utf-8"))

    KEY_FILE.write_bytes(key)
    PASSWORD_FILE.write_bytes(encrypted_password)

    # Restrict file permissions on Linux/macOS.
    os.chmod(KEY_FILE, 0o600)
    os.chmod(PASSWORD_FILE, 0o600)

    print("Password encrypted and saved successfully.")

    return password


PASSWORD = load_password()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

class Colors:
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    BLACK = "\033[30m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"
    REVERSED = "\033[7m"
    RESET = "\033[0m"

def log(msg: str, color: str = Colors.BLUE, ok: bool = True) -> None:
    mark = "v]" if ok else "x]"
    print(f"{color}{dt.datetime.now():%Y-%m-%d %H:%M:%S}    {mark} {msg}{Colors.RESET}")


class ScheduleError(Exception):
    """Raised for any expected failure, with a message already meant for the user."""


def get_token(url: str, username: str, password: str) -> str:
    if not username or not password:
        raise ScheduleError("Username or password is missing.")

    payload = {"Username": username, "Password": password, "Pin": "null"}

    try:
        resp = requests.post(
            url,
            json=payload,
            timeout=REQUEST_TIMEOUT,
            verify=False,
        )
    except requests.exceptions.ConnectionError as exc:
        raise ScheduleError(f"Couldn't connect to {url}: {exc}") from exc
    except requests.exceptions.Timeout as exc:
        raise ScheduleError(
            f"Login request to {url} timed out after {REQUEST_TIMEOUT}s."
        ) from exc

    if resp.status_code in (401, 403):
        raise ScheduleError(
            f"Login rejected ({resp.status_code}). Username or password is wrong "
            f"(or the account is locked). Server said: {resp.text.strip()[:200]!r}"
        )

    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        raise ScheduleError(
            f"Login failed: {exc}. Body: {resp.text.strip()[:200]!r}"
        ) from exc

    token = resp.text.strip('"').strip("'").strip()
    if not token:
        raise ScheduleError(
            f"Login succeeded (HTTP {resp.status_code}) but returned an empty token."
        )

    return token


def authed_session(token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})
    session.verify = False
    return session


def fetch_json(session: requests.Session, method: str, url: str, **kwargs):
    """GET/POST a URL and return parsed JSON, raising ScheduleError with context on failure."""
    try:
        resp = session.request(
            method,
            url,
            timeout=REQUEST_TIMEOUT,
            **kwargs,
        )
    except requests.exceptions.ConnectionError as exc:
        raise ScheduleError(f"Couldn't connect to {url}: {exc}") from exc
    except requests.exceptions.Timeout as exc:
        raise ScheduleError(
            f"Request to {url} timed out after {REQUEST_TIMEOUT}s."
        ) from exc

    if resp.status_code in (401, 403):
        raise ScheduleError(
            f"{url} rejected the request ({resp.status_code}) — token may have "
            f"expired or lacks permission. Body: {resp.text.strip()[:200]!r}"
        )

    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        raise ScheduleError(
            f"{url} returned an error: {exc}. Body: {resp.text.strip()[:200]!r}"
        ) from exc

    try:
        data = resp.json()
    except ValueError as exc:
        raise ScheduleError(
            f"{url} did not return valid JSON (got {resp.text.strip()[:200]!r})"
        ) from exc

    # Some endpoints on this API double-encode JSON as a string.
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ScheduleError(
                f"{url} returned a string that wasn't valid JSON either: {exc}"
            ) from exc

    return data


def merge_consecutive(lessons: list[dict]) -> list[dict]:
    """
    Merge back-to-back slots that share teacher/subject/room and are
    contiguous in time (e.g. two 30-min blocks -> one 60-min block).
    """
    lessons = sorted(lessons, key=lambda l: l["start"])
    merged: list[dict] = []

    for lesson in lessons:
        if merged:
            prev = merged[-1]
            same_class = (
                prev["TeacherName"] == lesson["TeacherName"]
                and prev["SubjectName"] == lesson["SubjectName"]
                and prev["RoomName"] == lesson["RoomName"]
            )
            contiguous = prev["end"] == lesson["start"]

            if same_class and contiguous:
                prev["end"] = lesson["end"]
                continue

        merged.append(dict(lesson))

    return merged


def print_day_schedule(lessons: list[dict], date: dt.date) -> None:
    """Pretty-print the day's schedule to the console."""
    day_lessons = [
        l for l in lessons
        if dt.datetime.fromisoformat(l["start"]).date() == date
    ]
    day_lessons.sort(key=lambda l: l["start"])

    header = f" Schedule for {date:%A, %d %B %Y} "
    print(f"\n{Colors.BLUE}{header.center(60, '=')}{Colors.RESET}")

    if not day_lessons:
        print(f"{Colors.YELLOW}No lessons found for this day.{Colors.RESET}\n")
        return

    for lesson in day_lessons:
        start = dt.datetime.fromisoformat(lesson["start"]).strftime("%H:%M")
        end = dt.datetime.fromisoformat(lesson["end"]).strftime("%H:%M")
        subject = lesson["SubjectName"] or "-"
        teacher = lesson["TeacherName"] or "-"
        room = lesson["RoomName"] or "-"

        print(
            f"{Colors.GREEN}{start}-{end}{Colors.RESET}  "
            f"{subject:<20} {teacher:<20} {room}"
        )

    print(f"{Colors.BLUE}{'=' * 60}{Colors.RESET}\n")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch and display school schedule."
    )
    parser.add_argument(
        "--date",
        type=lambda s: dt.datetime.strptime(s, "%Y-%m-%d").date(),
        default=dt.date.today(),
        help="Date to fetch/display (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--no-merge",
        action="store_true",
        help="Don't merge consecutive identical lesson slots.",
    )

    args = parser.parse_args()
    date = args.date

    os.makedirs(TEMP_FOLDER, exist_ok=True)
    os.makedirs(DATA_FOLDER, exist_ok=True)

    output_path = f"{DATA_FOLDER}/schedule-{date:%Y-%m-%d}.json"

    try:
        # Authentication
        log("Getting auth token...")
        jwt = get_token(AUTH_URL, USERNAME, PASSWORD)
        session = authed_session(jwt)

        # Schedule
        payload = {
            "schoolId": SCHOOL_ID,
            "from": f"{date}T01:00:00.000Z",
            "to": f"{date}T23:00:00.000Z",
            "getShortNames": False,
            "getAbsences": False,
        }

        log("Getting schedule...")
        parsed_schedule = fetch_json(
            session,
            "POST",
            f"{BASE_URL}//api/Schedule/GetScheduleCached",
            data=payload,
        )

    except ScheduleError as exc:
        log(str(exc), Colors.RED, ok=False)
        return 1

    except Exception as exc:
        log(f"Unexpected error: {exc!r}", Colors.RED, ok=False)
        raise

    # Raw dump for debugging.
    timestamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    raw_path = f"{TEMP_FOLDER}/schedule_{timestamp}.json"

    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(parsed_schedule, f, ensure_ascii=False, indent=4)

    lessons = parsed_schedule.get("lessons", [])
    filtered = [l for l in lessons if l.get("SubjectRelevantAbsences")]

    keys = ["TeacherName", "SubjectName", "RoomName", "start", "end"]
    simplified = [
        {k: entry.get(k) for k in keys}
        for entry in filtered
    ]

    if not args.no_merge:
        simplified = merge_consecutive(simplified)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(simplified, f, ensure_ascii=False, indent=4)

    log(f"Finished! Saved to {output_path}")
    print_day_schedule(simplified, date)

    return 0


if __name__ == "__main__":
    sys.exit(main())