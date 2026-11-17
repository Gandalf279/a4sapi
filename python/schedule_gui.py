"""Louisenlund weekly timetable and grades with a modern CustomTkinter interface.

Install dependencies with:
    python -m pip install -r requirements-schedule-gui.txt

Run with:
    python schedule_gui.py

TLS certificate verification is intentionally disabled to match the working
configuration requested by the user. This reduces connection security.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import customtkinter as ctk
import requests
import urllib3
import tkinter as tk
from tkinter import messagebox

# Certificate verification is intentionally disabled, as requested.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)



# ---------------------------------------------------------------------------
# API configuration
# ---------------------------------------------------------------------------
API_BASE = "https://schule.louisenlund.de/api"
AUTH_URL = f"{API_BASE}//Api/Authorization/JwtAuth"
SCHEDULE_URL = f"{API_BASE}//api/Schedule/GetScheduleCached"
GRADES_URL = f"{API_BASE}//Api/Dashboard/StudentGrade/GetFinalGradeDetails"
USER_INFO_URL = f"{API_BASE}//Api/AppUser/GetUserInfo"

SCHOOL_ID = 35
REQUEST_TIMEOUT = 15
TEMP_FOLDER = Path("temp")
DATA_FOLDER = Path("data")

# Hardcoded IDs for the grades request (taken from the web app's request).
STUDENT_CLASS_ID = 6527
SUBJECT_ID = 30


class ScheduleError(Exception):
    """An expected API or schedule-processing error."""


def get_token(username: str, password: str, pin: str) -> str:
    if not username.strip() or not password:
        raise ScheduleError("Please enter both your username and password.")

    payload = {
        "Username": username.strip(),
        "Password": password,
        "Pin": pin.strip() if pin.strip() else "null",
    }

    try:
        response = requests.post(
            AUTH_URL,
            json=payload,
            timeout=REQUEST_TIMEOUT,
            verify=False,
        )
    except requests.exceptions.SSLError as exc:
        raise ScheduleError(f"The login HTTPS request failed: {exc}") from exc
    except requests.exceptions.ConnectionError as exc:
        raise ScheduleError(f"Could not connect to the login server: {exc}") from exc
    except requests.exceptions.Timeout as exc:
        raise ScheduleError(
            f"The login request timed out after {REQUEST_TIMEOUT} seconds."
        ) from exc
    except requests.RequestException as exc:
        raise ScheduleError(f"The login request failed: {exc}") from exc

    if response.status_code in (401, 403):
        raise ScheduleError(
            "Login rejected. Check your username, password and PIN. "
            f"Server response: {response.text.strip()[:250]}"
        )

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise ScheduleError(
            f"Login failed ({response.status_code}): {response.text.strip()[:250]}"
        ) from exc

    token: str | None = None
    try:
        body = response.json()
        if isinstance(body, str):
            token = body.strip()
        elif isinstance(body, dict):
            for key in ("token", "Token", "access_token", "accessToken", "jwt"):
                value = body.get(key)
                if isinstance(value, str) and value.strip():
                    token = value.strip()
                    break
    except ValueError:
        pass

    if not token:
        token = response.text.strip().strip('"').strip("'").strip()
    if not token:
        raise ScheduleError("The login endpoint returned an empty token.")
    return token


def fetch_json(session: requests.Session, method: str, url: str, **kwargs: Any) -> Any:
    try:
        response = session.request(
            method,
            url,
            timeout=REQUEST_TIMEOUT,
            **kwargs,
        )
    except requests.exceptions.SSLError as exc:
        raise ScheduleError(f"The schedule HTTPS request failed: {exc}") from exc
    except requests.exceptions.ConnectionError as exc:
        raise ScheduleError(f"Could not connect to the schedule server: {exc}") from exc
    except requests.exceptions.Timeout as exc:
        raise ScheduleError(
            f"The schedule request timed out after {REQUEST_TIMEOUT} seconds."
        ) from exc
    except requests.RequestException as exc:
        raise ScheduleError(f"The schedule request failed: {exc}") from exc

    if response.status_code in (401, 403):
        raise ScheduleError(
            "The schedule server rejected the session. The token may have expired "
            f"or your account may not have permission. Server response: "
            f"{response.text.strip()[:250]}"
        )

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise ScheduleError(
            f"Schedule request failed ({response.status_code}): "
            f"{response.text.strip()[:250]}"
        ) from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise ScheduleError(
            "The schedule server did not return valid JSON. "
            f"Response: {response.text.strip()[:250]}"
        ) from exc

    # Some endpoints return JSON encoded inside a JSON string.
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ScheduleError(
                "The server returned a string that is not valid JSON."
            ) from exc
    return data


def merge_consecutive(lessons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge adjacent slots that have the same teacher, subject and room."""
    ordered = sorted(lessons, key=lambda lesson: str(lesson.get("start", "")))
    merged: list[dict[str, Any]] = []

    for lesson in ordered:
        if merged:
            previous = merged[-1]
            same_class = all(
                previous.get(key) == lesson.get(key)
                for key in ("TeacherName", "SubjectName", "RoomName")
            )
            previous_start = previous.get("start")
            lesson_start = lesson.get("start")
            same_day = (
                isinstance(previous_start, str)
                and isinstance(lesson_start, str)
                and previous_start[:10] == lesson_start[:10]
            )
            contiguous = previous.get("end") == lesson_start
            if same_class and same_day and contiguous:
                previous["end"] = lesson.get("end")
                continue
        merged.append(dict(lesson))
    return merged


def week_bounds(reference_date: dt.date) -> tuple[dt.date, dt.date]:
    """Return the Monday and Sunday for the week containing reference_date."""
    monday = reference_date - dt.timedelta(days=reference_date.weekday())
    return monday, monday + dt.timedelta(days=6)


def fetch_schedule(
    username: str,
    password: str,
    pin: str,
    reference_date: dt.date,
    filter_relevant: bool,
    merge_slots: bool,
    status_queue: queue.Queue[tuple[str, Any]],
) -> dict[str, Any]:
    """Fetch and save a full Monday-Sunday schedule. Runs in a background thread."""
    TEMP_FOLDER.mkdir(parents=True, exist_ok=True)
    DATA_FOLDER.mkdir(parents=True, exist_ok=True)

    status_queue.put(("status", "Signing in…"))
    token = get_token(username, password, pin)

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})
    session.verify = False

    week_start, week_end = week_bounds(reference_date)
    payload = {
        "schoolId": SCHOOL_ID,
        "from": f"{week_start.isoformat()}T01:00:00.000Z",
        "to": f"{week_end.isoformat()}T23:00:00.000Z",
        "getShortNames": False,
        "getAbsences": False,
    }

    status_queue.put(("status", f"Downloading week {week_start:%d %b}–{week_end:%d %b}…"))
    parsed_schedule = fetch_json(
        session,
        "POST",
        SCHEDULE_URL,
        data=payload,
    )
    if not isinstance(parsed_schedule, dict):
        raise ScheduleError("The schedule response has an unexpected format.")

    timestamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S%f")
    raw_path = TEMP_FOLDER / f"schedule_{timestamp}.json"
    with raw_path.open("w", encoding="utf-8") as file:
        json.dump(parsed_schedule, file, ensure_ascii=False, indent=4)

    lessons = parsed_schedule.get("lessons", [])
    if not isinstance(lessons, list):
        raise ScheduleError("The 'lessons' field in the API response is not a list.")

    if filter_relevant:
        lessons = [
            lesson
            for lesson in lessons
            if lesson.get("SubjectRelevantAbsences")
            or "gilde" in str(lesson.get("StudentClassName", "")).casefold()
        ]

    fields = (
        "TeacherName",
        "SubjectName",
        "RoomName",
        "Subject_Id",
        "StudentClass_Id",
        "start",
        "end",
    )
    simplified = [
        {field: lesson.get(field) for field in fields}
        for lesson in lessons
        if isinstance(lesson, dict)
    ]

    if merge_slots:
        simplified = merge_consecutive(simplified)
    else:
        simplified.sort(key=lambda lesson: str(lesson.get("start", "")))

    output_path = DATA_FOLDER / f"schedule-week-{week_start.isoformat()}.json"
    with output_path.open("w", encoding="utf-8") as file:
        json.dump(simplified, file, ensure_ascii=False, indent=4)

    return {
        "lessons": simplified,
        "output_path": str(output_path.resolve()),
        "raw_path": str(raw_path.resolve()),
        "week_start": week_start,
        "week_end": week_end,
    }


# ---------------------------------------------------------------------------
# Grades
# ---------------------------------------------------------------------------
def _weighted_mean(items: list[tuple[float, float]]) -> float | None:
    """Weighted mean of (value, weight) pairs; plain mean if all weights are 0."""
    if not items:
        return None
    total_weight = sum(weight for _, weight in items)
    if total_weight <= 0:
        return sum(value for value, _ in items) / len(items)
    return sum(value * weight for value, weight in items) / total_weight


def collect_subjects(lessons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return unique subject ids/names/class ids from schedule lesson data."""
    subjects: dict[int, dict[str, Any]] = {}
    for lesson in lessons:
        if not isinstance(lesson, dict):
            continue
        subject_id = lesson.get("Subject_Id")
        subject_name = lesson.get("SubjectName")
        if subject_name is None:
            continue
        try:
            subject_key = int(subject_id)
        except (TypeError, ValueError):
            continue
        name = str(subject_name).strip()
        if not name:
            continue
        normalized = name.casefold()
        if "veranstaltung" in normalized or "versammlung" in normalized:
            continue

        class_id = lesson.get("StudentClass_Id")
        try:
            class_key = int(class_id)
        except (TypeError, ValueError):
            class_key = None

        item = subjects.setdefault(
            subject_key,
            {"id": subject_key, "name": name, "class_id": class_key},
        )
        if class_key is not None and item.get("class_id") is None:
            item["class_id"] = class_key
        if item.get("name") == "":
            item["name"] = name
    return [
        {"id": item["id"], "name": item["name"], "class_id": item.get("class_id")}
        for item in sorted(subjects.values(), key=lambda item: item["id"])
    ]


def parse_grades(data: dict[str, Any], student_id: int) -> dict[str, Any]:
    """Build the category tree and averages from a GetFinalGradeDetails response.

    Category weights come from ``testCategories``. A category without graded
    entries is ignored, and the weights of its siblings are renormalised.
    """
    tests = {
        test.get("id"): test
        for test in data.get("tests", [])
        if isinstance(test, dict)
    }
    categories = {
        category.get("id"): category
        for category in data.get("testCategories", [])
        if isinstance(category, dict)
    }

    def test_value(test: dict[str, Any]) -> float | None:
        for grade in test.get("grades", []) or []:
            if (
                isinstance(grade, dict)
                and grade.get("studentId") == student_id
                and grade.get("isActive", True)
                and isinstance(grade.get("value"), (int, float))
            ):
                return float(grade["value"])
        return None

    def build(node: Any) -> dict[str, Any] | None:
        if not isinstance(node, dict) or node.get("isDeleted"):
            return None
        node_id = node.get("id")

        if node.get("structureType") == 1:
            test = tests.get(node_id)
            if test is None or test.get("active") is False:
                return None
            weight = test.get("weight")
            return {
                "kind": "test",
                "name": node.get("name") or test.get("title") or "Test",
                "date": node.get("date") or test.get("date"),
                "weight": float(weight) if isinstance(weight, (int, float)) else 1.0,
                "value": test_value(test),
                "children": [],
            }

        meta = categories.get(node_id, {})
        raw_weight = meta.get("weight")
        weight = (
            float(raw_weight)
            if isinstance(raw_weight, (int, float)) and meta.get("isWeightActive", True)
            else 1.0
        )
        children = [
            child
            for child in (build(item) for item in node.get("children", []) or [])
            if child is not None
        ]
        graded = [
            (child["value"], child["weight"])
            for child in children
            if child["value"] is not None
        ]
        return {
            "kind": "category",
            "name": node.get("name") or "Category",
            "date": None,
            "weight": weight,
            "value": _weighted_mean(graded),
            "children": children,
        }

    roots = [
        root
        for root in (build(item) for item in data.get("structure", []) or [])
        if root is not None
    ]
    computed = _weighted_mean(
        [(root["value"], root["weight"]) for root in roots if root["value"] is not None]
    )

    students = [s for s in data.get("students", []) if isinstance(s, dict)]
    student = next((s for s in students if s.get("id") == student_id), None)
    if student is None and students:
        student = students[0]
    student = student or {}

    def number(value: Any) -> float | None:
        return float(value) if isinstance(value, (int, float)) else None

    final_grade = number(student.get("gardeFinal"))
    manual_grade = number(student.get("gardeManuel"))
    calculated_grade = number(student.get("gardeCalculated"))
    if final_grade is not None:
        shown, source = final_grade, "Final grade"
    elif manual_grade is not None:
        shown, source = manual_grade, "Manual grade"
    elif calculated_grade is not None:
        shown, source = calculated_grade, "Calculated grade"
    else:
        shown, source = computed, "Calculated locally"

    def count_graded(nodes: list[dict[str, Any]]) -> int:
        total = 0
        for node in nodes:
            if node["kind"] == "test":
                total += 1 if node["value"] is not None else 0
            else:
                total += count_graded(node["children"])
        return total

    best = number(data.get("bestGrade"))
    worst = number(data.get("worstGrade"))
    return {
        "roots": roots,
        "shown": shown,
        "source": source,
        "student_name": str(student.get("name") or ""),
        "best": best if best is not None else 1.0,
        "worst": worst if worst is not None else 6.0,
        "graded_count": count_graded(roots),
    }


def fetch_student_id(session: requests.Session) -> int:
    """Read the student ID (crmEntityId) of the signed-in user from GetUserInfo."""
    info = fetch_json(session, "GET", USER_INFO_URL)
    if not isinstance(info, dict):
        raise ScheduleError("The user info response has an unexpected format.")
    value = info.get("crmEntityId")
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ScheduleError("The user info response contains no 'crmEntityId'.")
    try:
        return int(value)
    except ValueError as exc:
        raise ScheduleError(f"Invalid 'crmEntityId' in user info: {value!r}") from exc


def fetch_grades(
    username: str,
    password: str,
    pin: str,
    status_queue: queue.Queue[tuple[str, Any]],
    subject_specs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fetch and save the grade details for all relevant subjects."""
    DATA_FOLDER.mkdir(parents=True, exist_ok=True)

    if subject_specs is None:
        subject_specs = [{"id": SUBJECT_ID, "name": "Mathematik", "class_id": STUDENT_CLASS_ID}]

    normalized_subjects: list[tuple[int, str, int | None]] = []
    for subject in subject_specs:
        if not isinstance(subject, dict):
            continue
        raw_id = subject.get("id", subject.get("subject_id"))
        name = str(subject.get("name") or subject.get("subject_name") or "").strip()
        try:
            subject_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        class_id = subject.get("class_id", subject.get("studentClassId"))
        try:
            resolved_class_id = int(class_id)
        except (TypeError, ValueError):
            resolved_class_id = None
        if not name:
            name = f"Subject {subject_id}"
        normalized_subjects.append((subject_id, name, resolved_class_id))

    if not normalized_subjects:
        normalized_subjects = [(SUBJECT_ID, "Mathematik", STUDENT_CLASS_ID)]

    status_queue.put(("status", "Signing in…"))
    token = get_token(username, password, pin)

    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/plain, */*",
        }
    )
    session.verify = False

    status_queue.put(("status", "Looking up student ID…"))
    student_id = fetch_student_id(session)

    subject_rows: list[dict[str, Any]] = []
    for subject_id, subject_name, class_id in normalized_subjects:
        request_class_id = class_id if class_id is not None else STUDENT_CLASS_ID
        status_queue.put(("status", f"Downloading grades for {subject_name}…"))
        data = fetch_json(
            session,
            "GET",
            GRADES_URL,
            params={
                "studentClassId": request_class_id,
                "subjectId": subject_id,
                "studentId": student_id,
            },
        )
        if not isinstance(data, dict):
            raise ScheduleError("The grades response has an unexpected format.")

        output_path = DATA_FOLDER / f"grades-subject-{subject_id}.json"
        with output_path.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=4)

        summary = parse_grades(data, student_id)
        subject_rows.append(
            {
                "id": subject_id,
                "name": subject_name,
                "shown": summary["shown"],
                "summary": summary,
            }
        )

    values = [item["shown"] for item in subject_rows if item["shown"] is not None]
    overall = sum(values) / len(values) if values else None
    overall_summary = {
        "roots": [],
        "shown": overall,
        "source": "Average across subjects",
        "student_name": str(
            next(
                (
                    item["summary"]["student_name"]
                    for item in subject_rows
                    if item["summary"].get("student_name")
                ),
                "",
            )
        ),
        "best": 1.0,
        "worst": 6.0,
        "graded_count": sum(item["summary"].get("graded_count", 0) for item in subject_rows),
        "subjects": subject_rows,
    }

    output_path = DATA_FOLDER / "grades-all-subjects.json"
    with output_path.open("w", encoding="utf-8") as file:
        json.dump({"subjects": subject_rows, "overall": overall_summary}, file, ensure_ascii=False, indent=4)

    return {
        "summary": overall_summary,
        "output_path": str(output_path.resolve()),
    }


# ---------------------------------------------------------------------------
# Modern CustomTkinter interface
# ---------------------------------------------------------------------------
class ScheduleApp:
    DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

    LIGHT = {
        "window": "#F3F6FB",
        "card": "#FFFFFF",
        "surface": "#F8FAFC",
        "border": "#E3E9F2",
        "text": "#172033",
        "muted": "#6B7890",
        "accent": "#356AE6",
        "accent_hover": "#2857C7",
        "canvas": "#FFFFFF",
        "time": "#F2F5FA",
        "header": "#EAF0FA",
        "weekend": "#F8FAFD",
        "grid": "#DDE5F0",
        "minor_grid": "#EEF2F7",
        "lesson_text": "#26344D",
        "empty": "#A0AABC",
        "shadow": "#E8EDF5",
        "pill": "#EAF1FF",
        "highlight_morning": "#EEF5FF",
        "highlight_afternoon": "#FFF4E8",
    }
    DARK = {
        "window": "#0B1220",
        "card": "#111C2E",
        "surface": "#17243A",
        "border": "#293950",
        "text": "#E8EEF8",
        "muted": "#9AAAC0",
        "accent": "#6C97FF",
        "accent_hover": "#83A7FF",
        "canvas": "#101A2B",
        "time": "#19263A",
        "header": "#1B2B44",
        "weekend": "#142136",
        "grid": "#33445D",
        "minor_grid": "#26364D",
        "lesson_text": "#F4F7FC",
        "empty": "#71839D",
        "shadow": "#0A101B",
        "pill": "#20365B",
        "highlight_morning": "#172C4B",
        "highlight_afternoon": "#362A1D",
    }

    LIGHT_LESSON_COLORS = (
        ("#E1ECFF", "#8FB4F7"),
        ("#DDF5E8", "#83CBA3"),
        ("#FFF0D6", "#E9BE70"),
        ("#FBE2EC", "#E5A0BD"),
        ("#ECE6FF", "#B3A1F0"),
        ("#DDF3F4", "#83C9CE"),
        ("#FFE8D9", "#E8AE84"),
        ("#E8EDF4", "#AAB8CA"),
    )
    DARK_LESSON_COLORS = (
        ("#203B68", "#5486D8"),
        ("#1D493B", "#4B9C79"),
        ("#584326", "#B88C4B"),
        ("#512C42", "#B56B8D"),
        ("#3C3264", "#8674D0"),
        ("#1E4A50", "#4F9DA5"),
        ("#59402D", "#B68155"),
        ("#303E52", "#7187A4"),
    )

    def __init__(self, root: ctk.CTk) -> None:
        self.root = root
        self.root.title("Louisenlund · Weekly Schedule")
        self.root.geometry("1440x900")
        self.root.minsize(1080, 700)

        self.result_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.busy = False
        self.grades_busy = False
        self.last_output_path: Path | None = None
        self.current_lessons: list[dict[str, Any]] = []
        self.current_subjects: list[dict[str, Any]] = []
        self.current_week_start: dt.date | None = None
        self._grid_resize_job: str | None = None
        self._week_refresh_job: str | None = None
        self._clock_refresh_job: str | None = None
        self._refresh_after_busy = False
        self.colors = self.LIGHT
        self._grade_scale: tuple[float, float] = (1.0, 6.0)

        self.username_var = tk.StringVar()
        self.pin_var = tk.StringVar()
        self.date_var = tk.StringVar(value=dt.date.today().isoformat())
        self.week_range_var = tk.StringVar()
        self.filter_var = tk.BooleanVar(value=True)
        self.merge_var = tk.BooleanVar(value=True)
        self.show_password_var = tk.BooleanVar(value=False)
        self.appearance_var = tk.StringVar(value="System")
        self.status_var = tk.StringVar(value="Sign in and fetch your school timetable.")
        self.count_var = tk.StringVar(value="No schedule loaded")

        ctk.set_default_color_theme("blue")
        ctk.set_appearance_mode("System")
        self._build_layout()
        self.date_var.trace_add("write", self._update_week_label)
        self._update_week_label()
        self._apply_canvas_theme()
        self.root.after(120, self._poll_queue)

    def _build_layout(self) -> None:
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(1, weight=1)

        page = ctk.CTkFrame(self.root, corner_radius=0, fg_color="transparent")
        page.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=24, pady=(18, 20))
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(3, weight=1)

        # Header
        header = ctk.CTkFrame(page, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 18))
        header.grid_columnconfigure(0, weight=1)

        title_area = ctk.CTkFrame(header, fg_color="transparent")
        title_area.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            title_area,
            text="Louisenlund",
            font=ctk.CTkFont(family="Segoe UI", size=27, weight="bold"),
            text_color=("#172033", "#E8EEF8"),
        ).pack(anchor="w")
        ctk.CTkLabel(
            title_area,
            text="WEEKLY TIMETABLE & GRADES  ·  YOUR SCHOOL WEEK AT A GLANCE",
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            text_color=("#71809A", "#91A2BC"),
        ).pack(anchor="w", pady=(2, 0))

        appearance_area = ctk.CTkFrame(header, fg_color="transparent")
        appearance_area.grid(row=0, column=1, sticky="e")
        ctk.CTkLabel(
            appearance_area,
            text="Appearance",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=("#71809A", "#91A2BC"),
        ).pack(side="left", padx=(0, 9))
        self.appearance_menu = ctk.CTkOptionMenu(
            appearance_area,
            values=["System", "Light", "Dark"],
            variable=self.appearance_var,
            command=self._change_appearance,
            width=112,
            height=34,
            corner_radius=10,
            font=ctk.CTkFont(size=12, weight="bold"),
            dropdown_font=ctk.CTkFont(size=12),
        )
        self.appearance_menu.pack(side="left")

        # Compact, collapsible account/settings menu. Keep it closed by default
        # so the timetable receives almost all available vertical space.
        self.settings_open = False
        settings_shell = ctk.CTkFrame(page, fg_color="transparent")
        settings_shell.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        settings_shell.grid_columnconfigure(0, weight=1)
        self.settings_toggle = ctk.CTkButton(
            settings_shell,
            text="⚙  Account & settings  ▾",
            command=self._toggle_settings,
            width=176,
            height=32,
            corner_radius=9,
            fg_color=("#EAF0FA", "#253650"),
            hover_color=("#DCE6F5", "#304561"),
            text_color=("#31415B", "#DCE7F6"),
            font=ctk.CTkFont(size=11, weight="bold"),
            anchor="center",
        )
        self.settings_toggle.grid(row=0, column=0, sticky="e", pady=(0, 6))

        # Controls card (shown only while the menu is expanded)
        controls = ctk.CTkFrame(
            settings_shell,
            corner_radius=16,
            border_width=1,
            border_color=(self.LIGHT["border"], self.DARK["border"]),
            fg_color=(self.LIGHT["card"], self.DARK["card"]),
        )
        controls.grid(row=1, column=0, sticky="ew")
        controls.grid_columnconfigure((0, 1, 2, 3), weight=1)

        ctk.CTkLabel(
            controls,
            text="ACCOUNT & SETTINGS",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=("#74829A", "#91A2BC"),
        ).grid(row=0, column=0, columnspan=4, sticky="w", padx=18, pady=(14, 7))

        self.username_entry = self._make_labeled_entry(
            controls, "Username", row=1, column=0, placeholder="Your username",
            textvariable=self.username_var,
        )
        self.password_entry = self._make_labeled_entry(
            controls, "Password", row=1, column=1, placeholder="Your password", show="•"
        )
        self.pin_entry = self._make_labeled_entry(
            controls, "PIN (optional)", row=1, column=2, placeholder="Your PIN", show="•",
            textvariable=self.pin_var,
        )

        options = ctk.CTkFrame(controls, fg_color="transparent")
        options.grid(row=1, column=3, sticky="nsew", padx=(8, 18), pady=(0, 5))
        ctk.CTkLabel(
            options,
            text="OPTIONS",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=("#74829A", "#91A2BC"),
        ).pack(anchor="w", pady=(0, 6))
        self.show_password_check = ctk.CTkCheckBox(
            options,
            text="Show password",
            variable=self.show_password_var,
            command=self._toggle_password,
            font=ctk.CTkFont(size=11),
            checkbox_width=16,
            checkbox_height=16,
        )
        self.show_password_check.pack(anchor="w", pady=(1, 5))
        self.filter_check = ctk.CTkCheckBox(
            options,
            text="Absence-relevant only",
            variable=self.filter_var,
            font=ctk.CTkFont(size=11),
            checkbox_width=16,
            checkbox_height=16,
        )
        self.filter_check.pack(anchor="w", pady=1)
        self.merge_check = ctk.CTkCheckBox(
            options,
            text="Merge matching lessons",
            variable=self.merge_var,
            font=ctk.CTkFont(size=11),
            checkbox_width=16,
            checkbox_height=16,
        )
        self.merge_check.pack(anchor="w", pady=(5, 0))

        separator = ctk.CTkFrame(controls, height=1, fg_color=("#E9EDF4", "#293950"))
        separator.grid(row=2, column=0, columnspan=4, sticky="ew", padx=18, pady=(10, 12))

        bottom = ctk.CTkFrame(controls, fg_color="transparent")
        bottom.grid(row=3, column=0, columnspan=4, sticky="ew", padx=18, pady=(0, 15))
        bottom.grid_columnconfigure(1, weight=1)

        week_controls = ctk.CTkFrame(bottom, fg_color="transparent")
        week_controls.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            week_controls,
            text="WEEK OF",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=("#74829A", "#91A2BC"),
        ).pack(anchor="w", pady=(0, 5))
        date_row = ctk.CTkFrame(week_controls, fg_color="transparent")
        date_row.pack(anchor="w")
        self.date_entry = ctk.CTkEntry(
            date_row,
            textvariable=self.date_var,
            width=128,
            height=36,
            corner_radius=9,
            font=ctk.CTkFont(size=12),
            placeholder_text="YYYY-MM-DD",
        )
        self.date_entry.pack(side="left", padx=(0, 6))
        ctk.CTkLabel(
            week_controls,
            text="Changing this date updates the displayed week label. Use the arrows above the timetable to refresh a different week.",
            font=ctk.CTkFont(size=10),
            text_color=("#74829A", "#91A2BC"),
            wraplength=280,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

        action_area = ctk.CTkFrame(bottom, fg_color="transparent")
        action_area.grid(row=0, column=2, sticky="e", padx=(12, 0))
        self.open_folder_button = ctk.CTkButton(
            action_area,
            text="Open data folder",
            command=self._open_data_folder,
            width=132,
            height=38,
            corner_radius=10,
            fg_color=("#EAF0FA", "#253650"),
            hover_color=("#DCE6F5", "#304561"),
            text_color=("#31415B", "#DCE7F6"),
            font=ctk.CTkFont(size=11, weight="bold"),
        )
        self.open_folder_button.pack(side="left", padx=(0, 8))
        self.fetch_button = ctk.CTkButton(
            action_area,
            text="↻   Fetch whole week",
            command=self._start_fetch,
            width=180,
            height=38,
            corner_radius=10,
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.fetch_button.pack(side="left")

        controls.grid_remove()

        # Persistent toolbar: week navigation (left) and view switch (right).
        week_toolbar = ctk.CTkFrame(page, fg_color="transparent")
        week_toolbar.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        week_toolbar.grid_columnconfigure(1, weight=1)

        # Keep the previous/range/next controls grouped at the left edge.
        self.nav_frame = ctk.CTkFrame(week_toolbar, fg_color="transparent")
        self.nav_frame.grid(row=0, column=0, sticky="w")

        self.prev_button = ctk.CTkButton(
            self.nav_frame,
            text="‹",
            command=lambda: self._shift_week(-7),
            width=38,
            height=34,
            corner_radius=9,
            font=ctk.CTkFont(size=21, weight="bold"),
            fg_color=("#EAF0FA", "#253650"),
            hover_color=("#DCE6F5", "#304561"),
            text_color=("#31415B", "#DCE7F6"),
        )
        self.prev_button.grid(row=0, column=0, sticky="w", padx=(0, 6))

        ctk.CTkLabel(
            self.nav_frame,
            textvariable=self.week_range_var,
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=("#53627A", "#B0BED2"),
            anchor="w",
        ).grid(row=0, column=1, sticky="w", padx=(4, 10))

        self.next_button = ctk.CTkButton(
            self.nav_frame,
            text="›",
            command=lambda: self._shift_week(7),
            width=38,
            height=34,
            corner_radius=9,
            font=ctk.CTkFont(size=21, weight="bold"),
            fg_color=("#EAF0FA", "#253650"),
            hover_color=("#DCE6F5", "#304561"),
            text_color=("#31415B", "#DCE7F6"),
        )
        self.next_button.grid(row=0, column=2, sticky="w")

        self.view_switch = ctk.CTkSegmentedButton(
            week_toolbar,
            values=["Timetable", "Grades"],
            command=self._switch_view,
            height=34,
            corner_radius=9,
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.view_switch.set("Timetable")
        self.view_switch.grid(row=0, column=1, sticky="e")

        # Timetable card
        timetable = ctk.CTkFrame(
            page,
            corner_radius=16,
            border_width=1,
            border_color=(self.LIGHT["border"], self.DARK["border"]),
            fg_color=(self.LIGHT["card"], self.DARK["card"]),
        )
        timetable.grid(row=3, column=0, sticky="nsew")
        timetable.grid_columnconfigure(0, weight=1)
        timetable.grid_rowconfigure(1, weight=1)
        self.timetable_card = timetable

        timetable_header = ctk.CTkFrame(timetable, fg_color="transparent")
        timetable_header.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 10))
        timetable_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            timetable_header,
            text="Your timetable",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=(self.LIGHT["text"], self.DARK["text"]),
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            timetable_header,
            text="MONDAY — SUNDAY",
            font=ctk.CTkFont(size=9, weight="bold"),
            text_color=("#7D8BA2", "#90A2BC"),
        ).grid(row=0, column=1, sticky="e")

        grid_shell = ctk.CTkFrame(
            timetable,
            corner_radius=11,
            fg_color=("#F8FAFD", "#0D1727"),
            border_width=1,
            border_color=(self.LIGHT["border"], self.DARK["border"]),
        )
        grid_shell.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 12))
        grid_shell.grid_columnconfigure(0, weight=1)
        grid_shell.grid_rowconfigure(0, weight=1)

        self.grid_canvas = tk.Canvas(
            grid_shell,
            background=self.LIGHT["canvas"],
            highlightthickness=0,
            borderwidth=0,
            relief="flat",
        )
        self.vertical_scroll = ctk.CTkScrollbar(
            grid_shell, orientation="vertical", command=self.grid_canvas.yview
        )
        self.horizontal_scroll = ctk.CTkScrollbar(
            grid_shell, orientation="horizontal", command=self.grid_canvas.xview
        )
        self.grid_canvas.configure(
            yscrollcommand=self.vertical_scroll.set,
            xscrollcommand=self.horizontal_scroll.set,
        )
        self.grid_canvas.grid(row=0, column=0, sticky="nsew", padx=(4, 0), pady=(4, 0))
        self.vertical_scroll.grid(row=0, column=1, sticky="ns", padx=(0, 4), pady=4)
        self.horizontal_scroll.grid(row=1, column=0, sticky="ew", padx=4, pady=(0, 4))
        self.grid_canvas.bind("<Configure>", self._on_grid_resize)
        self.grid_canvas.bind("<MouseWheel>", self._on_grid_mousewheel)
        self.grid_canvas.bind("<Button-4>", lambda _event: self.grid_canvas.yview_scroll(-1, "units"))
        self.grid_canvas.bind("<Button-5>", lambda _event: self.grid_canvas.yview_scroll(1, "units"))

        # Grades card (same grid cell as the timetable; toggled by the view switch)
        grades = ctk.CTkFrame(
            page,
            corner_radius=16,
            border_width=1,
            border_color=(self.LIGHT["border"], self.DARK["border"]),
            fg_color=(self.LIGHT["card"], self.DARK["card"]),
        )
        grades.grid(row=3, column=0, sticky="nsew")
        grades.grid_columnconfigure(0, weight=1)
        grades.grid_rowconfigure(1, weight=1)
        self.grades_card = grades

        grades_header = ctk.CTkFrame(grades, fg_color="transparent")
        grades_header.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 10))
        grades_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            grades_header,
            text="Your grades",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=(self.LIGHT["text"], self.DARK["text"]),
        ).grid(row=0, column=0, sticky="w")
        self.grades_button = ctk.CTkButton(
            grades_header,
            text="↻   Load grades",
            command=self._start_grades_fetch,
            width=150,
            height=34,
            corner_radius=10,
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.grades_button.grid(row=0, column=1, sticky="e")

        self.grades_body = ctk.CTkScrollableFrame(
            grades,
            fg_color="transparent",
            corner_radius=0,
        )
        self.grades_body.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 12))
        self.grades_body.grid_columnconfigure(0, weight=1)
        self._render_grades_placeholder(
            "No grades loaded. Open Account & settings, enter the login details, "
            "then press Load grades."
        )
        grades.grid_remove()

        footer = ctk.CTkFrame(page, fg_color="transparent")
        footer.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        footer.grid_columnconfigure(0, weight=1)
        self.status_label = ctk.CTkLabel(
            footer,
            textvariable=self.status_var,
            font=ctk.CTkFont(size=11),
            text_color=("#64748B", "#A0AFC5"),
            anchor="w",
        )
        self.status_label.grid(row=0, column=0, sticky="ew")
        self.count_label = ctk.CTkLabel(
            footer,
            textvariable=self.count_var,
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=("#64748B", "#A0AFC5"),
        )
        self.count_label.grid(row=0, column=1, sticky="e", padx=(12, 0))
        self.progress = ctk.CTkProgressBar(
            footer,
            height=5,
            corner_radius=4,
            mode="indeterminate",
            progress_color=(self.LIGHT["accent"], self.DARK["accent"]),
        )
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.progress.set(0)

    def _toggle_settings(self) -> None:
        """Expand/collapse account details and schedule options."""
        self.settings_open = not self.settings_open
        if self.settings_open:
            self.settings_toggle.configure(text="⚙  Account & settings  ▴")
            # The settings card is the second child in the shell.
            self.settings_toggle.master.winfo_children()[1].grid()
        else:
            self.settings_toggle.configure(text="⚙  Account & settings  ▾")
            self.settings_toggle.master.winfo_children()[1].grid_remove()
        self.root.after(80, self._on_grid_resize, None)

    def _switch_view(self, value: str) -> None:
        """Show either the weekly timetable or the grades overview."""
        if value == "Grades":
            self.timetable_card.grid_remove()
            self.nav_frame.grid_remove()
            self.grades_card.grid()
        else:
            self.grades_card.grid_remove()
            self.timetable_card.grid()
            self.nav_frame.grid()
            self.root.after(80, self._on_grid_resize, None)

    def _make_labeled_entry(
        self,
        parent: ctk.CTkFrame,
        label: str,
        row: int,
        column: int,
        placeholder: str,
        show: str | None = None,
        textvariable: tk.StringVar | None = None,
    ) -> ctk.CTkEntry:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=row, column=column, sticky="ew", padx=(18 if column == 0 else 8, 8), pady=(0, 5))
        frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            frame,
            text=label,
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=("#74829A", "#91A2BC"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 6))
        entry = ctk.CTkEntry(
            frame,
            height=36,
            corner_radius=9,
            placeholder_text=placeholder,
            show=show or "",
            textvariable=textvariable,
            font=ctk.CTkFont(size=12),
        )
        entry.grid(row=1, column=0, sticky="ew")
        return entry

    def _change_appearance(self, mode: str) -> None:
        ctk.set_appearance_mode(mode)
        self.root.after(80, self._apply_canvas_theme)

    def _apply_canvas_theme(self) -> None:
        mode = ctk.get_appearance_mode()
        self.colors = self.DARK if mode == "Dark" else self.LIGHT
        self.grid_canvas.configure(background=self.colors["canvas"])
        self._draw_week_grid()

    def _toggle_password(self) -> None:
        self.password_entry.configure(show="" if self.show_password_var.get() else "•")

    def _start_fetch(self) -> None:
        if self.busy:
            return
        username = self.username_var.get().strip()
        password = self.password_entry.get()
        pin = self.pin_var.get()
        if not username or not password:
            messagebox.showwarning(
                "Missing login details", "Please enter your username and password."
            )
            return
        try:
            selected_date = dt.datetime.strptime(
                self.date_var.get().strip(), "%Y-%m-%d"
            ).date()
        except ValueError:
            messagebox.showwarning("Invalid date", "Enter the date in YYYY-MM-DD format.")
            return

        self.busy = True
        self.fetch_button.configure(state="disabled", text="Fetching…")
        self.progress.start()
        week_start, week_end = week_bounds(selected_date)
        self.status_var.set(f"Preparing week {week_start:%d %b}-{week_end:%d %b}…")
        self.count_var.set("Fetching schedule…")

        worker = threading.Thread(
            target=self._worker,
            args=(
                username,
                password,
                pin,
                selected_date,
                self.filter_var.get(),
                self.merge_var.get(),
            ),
            daemon=True,
        )
        worker.start()

    def _worker(
        self,
        username: str,
        password: str,
        pin: str,
        selected_date: dt.date,
        filter_relevant: bool,
        merge_slots: bool,
    ) -> None:
        try:
            result = fetch_schedule(
                username,
                password,
                pin,
                selected_date,
                filter_relevant,
                merge_slots,
                self.result_queue,
            )
            self.result_queue.put(("success", result))
        except ScheduleError as exc:
            self.result_queue.put(("error", str(exc)))
        except Exception as exc:  # Keep unexpected worker exceptions visible in the GUI.
            self.result_queue.put(("error", f"Unexpected error: {exc}"))

    # -- grades ------------------------------------------------------------
    def _start_grades_fetch(self) -> None:
        if self.grades_busy:
            return
        username = self.username_var.get().strip()
        password = self.password_entry.get()
        pin = self.pin_var.get()
        if not username or not password:
            messagebox.showwarning(
                "Missing login details",
                "Open Account & settings and enter your username and password.",
            )
            return
        self.grades_busy = True
        self.grades_button.configure(state="disabled", text="Loading…")
        if not self.busy:
            self.progress.start()
        self.status_var.set("Preparing grades request…")

        worker = threading.Thread(
            target=self._grades_worker,
            args=(username, password, pin, self.current_subjects),
            daemon=True,
        )
        worker.start()

    def _grades_worker(
        self,
        username: str,
        password: str,
        pin: str,
        subject_specs: list[dict[str, Any]],
    ) -> None:
        try:
            result = fetch_grades(username, password, pin, self.result_queue, subject_specs)
            self.result_queue.put(("grades_success", result))
        except ScheduleError as exc:
            self.result_queue.put(("grades_error", str(exc)))
        except Exception as exc:  # Keep unexpected worker exceptions visible in the GUI.
            self.result_queue.put(("grades_error", f"Unexpected error: {exc}"))

    def _finish_grades_busy(self) -> None:
        self.grades_busy = False
        self.grades_button.configure(state="normal", text="↻   Load grades")
        if not self.busy:
            self.progress.stop()
            self.progress.set(0)

    def _show_grades(self, result: dict[str, Any]) -> None:
        self._finish_grades_busy()
        summary: dict[str, Any] = result["summary"]
        self._grade_scale = (summary["best"], summary["worst"])
        self._render_grades(summary)
        self.status_var.set(f"Saved grades  ·  {result['output_path']}")
        self.count_var.set(f"{summary['graded_count']} graded entries")

    @staticmethod
    def _format_grade(value: float | None, decimals: int = 2) -> str:
        if value is None:
            return "–"
        return f"{value:.{decimals}f}"

    @staticmethod
    def _format_test_grade(value: float | None) -> str:
        if value is None:
            return "–"
        return f"{value:.1f}" if round(value, 1) == round(value, 6) else f"{value:.2f}"

    def _grade_color(self, value: float | None) -> tuple[str, str]:
        """Return (light, dark) text colours for a grade on the active scale."""
        if value is None:
            return ("#A0AABC", "#71839D")
        best, worst = self._grade_scale
        span = worst - best
        ratio = (value - best) / span if span else 0.0
        if ratio <= 0.1:
            return ("#1E8E5A", "#4FD08C")
        if ratio <= 0.3:
            return ("#2F6FE0", "#6C97FF")
        if ratio <= 0.5:
            return ("#B7791F", "#E3B055")
        if ratio <= 0.7:
            return ("#C2570C", "#F08F4A")
        return ("#C62F3F", "#F06C7A")

    def _clear_grades_body(self) -> None:
        for child in self.grades_body.winfo_children():
            child.destroy()

    def _render_grades_placeholder(self, text: str) -> None:
        self._clear_grades_body()
        ctk.CTkLabel(
            self.grades_body,
            text=text,
            font=ctk.CTkFont(size=12),
            text_color=("#74829A", "#91A2BC"),
            wraplength=560,
            justify="left",
        ).pack(anchor="w", padx=8, pady=14)

    def _render_grades(self, summary: dict[str, Any]) -> None:
        self._clear_grades_body()

        # Final grade tile
        tile = ctk.CTkFrame(
            self.grades_body,
            corner_radius=14,
            border_width=1,
            border_color=(self.LIGHT["border"], self.DARK["border"]),
            fg_color=(self.LIGHT["surface"], self.DARK["surface"]),
        )
        tile.pack(fill="x", padx=4, pady=(2, 12))
        tile.grid_columnconfigure(1, weight=1)

        shown: float | None = summary["shown"]
        ctk.CTkLabel(
            tile,
            text=self._format_grade(shown, 2),
            font=ctk.CTkFont(size=44, weight="bold"),
            text_color=self._grade_color(shown),
        ).grid(row=0, column=0, rowspan=2, padx=(22, 18), pady=14)
        ctk.CTkLabel(
            tile,
            text=summary["source"].upper(),
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=("#74829A", "#91A2BC"),
            anchor="w",
        ).grid(row=0, column=1, sticky="sw", pady=(16, 0))
        detail_parts = []
        if summary["student_name"]:
            detail_parts.append(summary["student_name"])
        if shown is not None:
            detail_parts.append(f"exact {shown:.3f}")
        detail_parts.append(
            f"scale {summary['best']:g} (best) – {summary['worst']:g} (worst)"
        )
        ctk.CTkLabel(
            tile,
            text="  ·  ".join(detail_parts),
            font=ctk.CTkFont(size=12),
            text_color=(self.LIGHT["text"], self.DARK["text"]),
            anchor="w",
        ).grid(row=1, column=1, sticky="nw", pady=(2, 16))

        subject_rows = summary.get("subjects")
        if subject_rows:
            header = ctk.CTkLabel(
                self.grades_body,
                text="SUBJECT AVERAGES",
                font=ctk.CTkFont(size=10, weight="bold"),
                text_color=("#74829A", "#91A2BC"),
                anchor="w",
            )
            header.pack(anchor="w", padx=8, pady=(0, 8))
            for entry in subject_rows:
                subject_name = str(entry.get("name") or "Subject")
                subject_value = entry.get("shown")
                row = ctk.CTkFrame(
                    self.grades_body,
                    corner_radius=10,
                    border_width=1,
                    border_color=(self.LIGHT["border"], self.DARK["border"]),
                    fg_color=(self.LIGHT["card"], self.DARK["card"]),
                )
                row.pack(fill="x", padx=4, pady=3)
                row.grid_columnconfigure(0, weight=1)
                ctk.CTkLabel(
                    row,
                    text=subject_name,
                    font=ctk.CTkFont(size=13),
                    text_color=(self.LIGHT["text"], self.DARK["text"]),
                    anchor="w",
                ).grid(row=0, column=0, sticky="w", padx=(14, 8), pady=(10, 10))
                ctk.CTkLabel(
                    row,
                    text=self._format_grade(subject_value, 2),
                    font=ctk.CTkFont(size=15, weight="bold"),
                    text_color=self._grade_color(subject_value),
                    width=68,
                    anchor="e",
                ).grid(row=0, column=1, sticky="e", padx=(0, 14), pady=10)
            return

        if not summary["roots"]:
            ctk.CTkLabel(
                self.grades_body,
                text="The response contains no grade structure.",
                font=ctk.CTkFont(size=12),
                text_color=("#74829A", "#91A2BC"),
            ).pack(anchor="w", padx=8, pady=8)
            return

        for root in summary["roots"]:
            self._add_grade_node(root, 0)

    def _add_grade_node(self, node: dict[str, Any], depth: int) -> None:
        is_test = node["kind"] == "test"
        row = ctk.CTkFrame(
            self.grades_body,
            corner_radius=10,
            border_width=1 if is_test else 0,
            border_color=(self.LIGHT["border"], self.DARK["border"]),
            fg_color=(
                (self.LIGHT["card"], self.DARK["card"])
                if is_test
                else (self.LIGHT["header"], self.DARK["header"])
                if depth == 0
                else (self.LIGHT["surface"], self.DARK["surface"])
            ),
        )
        row.pack(fill="x", padx=(4 + depth * 22, 4), pady=3)
        row.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            row,
            text=str(node["name"]),
            font=ctk.CTkFont(size=13 if depth == 0 and not is_test else 12, weight="normal" if is_test else "bold"),
            text_color=(self.LIGHT["text"], self.DARK["text"]),
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=(14, 8), pady=(9, 0 if is_test else 9))

        if is_test:
            date = self._parse_schedule_datetime(node.get("date"))
            subtitle = date.strftime("%d %b %Y") if date else ""
            if node["weight"] != 1.0:
                subtitle += f"{'  ·  ' if subtitle else ''}weight ×{node['weight']:g}"
            if subtitle:
                ctk.CTkLabel(
                    row,
                    text=subtitle,
                    font=ctk.CTkFont(size=10),
                    text_color=("#74829A", "#91A2BC"),
                    anchor="w",
                ).grid(row=1, column=0, sticky="w", padx=(14, 8), pady=(0, 9))
            value_text = self._format_test_grade(node["value"])
        else:
            ctk.CTkLabel(
                row,
                text=f"{node['weight']:g} %",
                font=ctk.CTkFont(size=10, weight="bold"),
                text_color=("#53627A", "#B0BED2"),
                fg_color=(self.LIGHT["pill"], self.DARK["pill"]),
                corner_radius=8,
                width=52,
                height=22,
            ).grid(row=0, column=1, sticky="e", padx=(0, 10), pady=9)
            value_text = self._format_grade(node["value"], 2)

        ctk.CTkLabel(
            row,
            text=value_text,
            font=ctk.CTkFont(size=16 if is_test else 15, weight="bold"),
            text_color=self._grade_color(node["value"]),
            width=56,
            anchor="e",
        ).grid(row=0, column=2, rowspan=2 if is_test else 1, sticky="e", padx=(0, 14), pady=9)

        for child in node["children"]:
            self._add_grade_node(child, depth + 1)

    # -- schedule ----------------------------------------------------------
    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.result_queue.get_nowait()
                if kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "success":
                    self._show_result(payload)
                elif kind == "error":
                    self._finish_busy()
                    self.status_var.set("Could not load the schedule.")
                    self.count_var.set("Request failed")
                    messagebox.showerror("Schedule error", str(payload))
                elif kind == "grades_success":
                    self._show_grades(payload)
                elif kind == "grades_error":
                    self._finish_grades_busy()
                    self.status_var.set("Could not load the grades.")
                    self.count_var.set("Request failed")
                    messagebox.showerror("Grades error", str(payload))
        except queue.Empty:
            pass
        self.root.after(120, self._poll_queue)

    def _show_result(self, result: dict[str, Any]) -> None:
        self._finish_busy()
        self.current_lessons = sorted(
            result["lessons"], key=lambda lesson: str(lesson.get("start", ""))
        )
        self.current_subjects = collect_subjects(self.current_lessons)
        self.current_week_start = result["week_start"]
        self._draw_week_grid()
        self._schedule_clock_tick()
        week_start: dt.date = result["week_start"]
        week_end: dt.date = result["week_end"]
        self.last_output_path = Path(result["output_path"])
        self.status_var.set(
            f"Saved {week_start:%d %b}–{week_end:%d %b %Y}  ·  {result['output_path']}"
        )
        self.count_var.set(f"{len(self.current_lessons)} lessons · 7 days")

    def _schedule_clock_tick(self) -> None:
        """Refresh the current-time marker every 15 seconds without re-fetching data."""
        if self._clock_refresh_job is not None:
            try:
                self.root.after_cancel(self._clock_refresh_job)
            except tk.TclError:
                pass
        self._clock_refresh_job = self.root.after(15000, self._update_clock_marker)

    def _update_clock_marker(self) -> None:
        self._clock_refresh_job = None
        if self.current_week_start is not None:
            self._draw_week_grid()
        self._schedule_clock_tick()

    def _finish_busy(self) -> None:
        self.busy = False
        self.fetch_button.configure(state="normal", text="↻   Fetch whole week")
        if not self.grades_busy:
            self.progress.stop()
            self.progress.set(0)
        if self._refresh_after_busy:
            self._refresh_after_busy = False
            self.root.after(0, self._start_fetch)

    def _on_grid_resize(self, _event: tk.Event | None) -> None:
        if self.current_week_start is None:
            return
        if self._grid_resize_job is not None:
            try:
                self.root.after_cancel(self._grid_resize_job)
            except tk.TclError:
                pass
        self._grid_resize_job = self.root.after(100, self._draw_week_grid)

    def _on_grid_mousewheel(self, event: tk.Event) -> str:
        if event.state & 0x0001:
            self.grid_canvas.xview_scroll(int(-event.delta / 120), "units")
        else:
            self.grid_canvas.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    @staticmethod
    def _parse_schedule_datetime(value: Any) -> dt.datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _rounded_rectangle(
        canvas: tk.Canvas,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        radius: float,
        **kwargs: Any,
    ) -> int:
        """Draw a smooth rounded rectangle on a Tk canvas."""
        radius = max(2, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
        points = [
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1,
        ]
        return canvas.create_polygon(points, smooth=True, splinesteps=16, **kwargs)

    def _draw_week_grid(self) -> None:
        """Render a modern WebUntis-style timetable on the scrollable canvas."""
        self._grid_resize_job = None
        if self.current_week_start is None:
            return

        canvas = self.grid_canvas
        canvas.delete("all")
        palette = self.colors
        week_start = self.current_week_start
        time_col_width = 76
        header_height = 62
        hour_height = 64
        canvas_width = max(canvas.winfo_width(), 1120)
        day_width = max(142, (canvas_width - time_col_width - 12) / 7)
        total_width = time_col_width + day_width * 7

        parsed: dict[dt.date, list[tuple[int, int, dict[str, Any]]]] = {}
        # Keep the visible timetable anchored at 08:00, regardless of any
        # earlier lesson timestamps returned by the API.
        earliest = 8 * 60
        latest = 18 * 60
        for lesson in self.current_lessons:
            start_dt = self._parse_schedule_datetime(lesson.get("start"))
            end_dt = self._parse_schedule_datetime(lesson.get("end"))
            if start_dt is None or end_dt is None:
                continue
            lesson_day = start_dt.date()
            if not (week_start <= lesson_day <= week_start + dt.timedelta(days=6)):
                continue
            start_minute = start_dt.hour * 60 + start_dt.minute
            end_minute = end_dt.hour * 60 + end_dt.minute
            if end_minute <= start_minute:
                end_minute += 24 * 60
            parsed.setdefault(lesson_day, []).append((start_minute, end_minute, lesson))
            earliest = min(earliest, start_minute)
            latest = max(latest, end_minute)

        # Give the timetable a little breathing room before the first and after the last lesson.
        first_hour = 8
        last_hour = min(24, max(9, (latest + 59) // 60 + 1))
        if last_hour <= first_hour:
            last_hour = min(24, first_hour + 1)
        total_height = header_height + (last_hour - first_hour) * hour_height
        start_absolute = first_hour * 60
        end_absolute = last_hour * 60

        canvas.create_rectangle(
            0, 0, total_width, total_height,
            fill=palette["canvas"], outline=palette["border"], width=1,
        )
        canvas.create_rectangle(
            0, 0, time_col_width, header_height,
            fill=palette["time"], outline=palette["border"],
        )
        canvas.create_text(
            time_col_width / 2,
            header_height / 2,
            text="TIME",
            fill=palette["muted"],
            font=("Segoe UI", 9, "bold"),
        )

        today = dt.date.today()
        for offset, day_name in enumerate(self.DAY_NAMES):
            day = week_start + dt.timedelta(days=offset)
            x0 = time_col_width + offset * day_width
            x1 = x0 + day_width
            is_weekend = offset >= 5
            is_today = day == today
            header_fill = palette["pill"] if is_today else palette["header"]
            body_fill = palette["weekend"] if is_weekend else palette["canvas"]
            canvas.create_rectangle(
                x0, header_height, x1, total_height,
                fill=body_fill, outline=palette["border"], width=1,
            )
            for range_start, range_end, range_fill in (
                (8 * 60, 11 * 60 + 30, palette["highlight_morning"]),
                (15 * 60, 16 * 60 + 30, palette["highlight_afternoon"]),
            ):
                visible_start = max(range_start, start_absolute)
                visible_end = min(range_end, end_absolute)
                if visible_end <= visible_start:
                    continue
                y0 = header_height + ((visible_start - start_absolute) / 60) * hour_height
                y1 = header_height + ((visible_end - start_absolute) / 60) * hour_height
                canvas.create_rectangle(
                    x0, y0, x1, y1,
                    fill=range_fill,
                    outline="",
                    width=0,
                )
            canvas.create_rectangle(
                x0, 0, x1, header_height,
                fill=header_fill, outline=palette["border"], width=1,
            )
            canvas.create_text(
                (x0 + x1) / 2,
                20,
                text=day_name.upper(),
                fill=palette["accent"] if is_today else palette["text"],
                font=("Segoe UI", 9, "bold"),
            )
            canvas.create_text(
                (x0 + x1) / 2,
                43,
                text=day.strftime("%d %b %Y"),
                fill=palette["muted"],
                font=("Segoe UI", 8),
            )

        for minute in range(start_absolute, end_absolute + 1, 30):
            y = header_height + ((minute - start_absolute) / 60) * hour_height
            is_full_hour = minute % 60 == 0
            canvas.create_line(
                time_col_width,
                y,
                total_width,
                y,
                fill=palette["grid"] if is_full_hour else palette["minor_grid"],
                width=1,
                dash=() if is_full_hour else (2, 3),
            )
            if is_full_hour and minute < end_absolute:
                canvas.create_text(
                    time_col_width - 10,
                    y + 2,
                    text=f"{minute // 60:02d}:00",
                    anchor="ne",
                    fill=palette["muted"],
                    font=("Segoe UI", 8),
                )

        lesson_colors = (
            self.DARK_LESSON_COLORS
            if ctk.get_appearance_mode() == "Dark"
            else self.LIGHT_LESSON_COLORS
        )
        for offset in range(7):
            day = week_start + dt.timedelta(days=offset)
            day_lessons = sorted(parsed.get(day, []), key=lambda item: (item[0], item[1]))
            column_x = time_col_width + offset * day_width
            if not day_lessons:
                canvas.create_text(
                    column_x + day_width / 2,
                    header_height + 34,
                    text="No lessons",
                    fill=palette["empty"],
                    font=("Segoe UI", 9, "italic"),
                )
                continue

            # Greedy interval packing separates simultaneous lessons into lanes.
            lane_ends: list[int] = []
            placed: list[tuple[int, int, dict[str, Any], int]] = []
            for start_minute, end_minute, lesson in day_lessons:
                lane = next(
                    (index for index, lane_end in enumerate(lane_ends) if lane_end <= start_minute),
                    len(lane_ends),
                )
                if lane == len(lane_ends):
                    lane_ends.append(end_minute)
                else:
                    lane_ends[lane] = end_minute
                placed.append((start_minute, end_minute, lesson, lane))

            lane_count = max(1, len(lane_ends))
            lane_width = day_width / lane_count
            for start_minute, end_minute, lesson, lane in placed:
                x0 = column_x + lane * lane_width + 4
                x1 = column_x + (lane + 1) * lane_width - 4
                y0 = header_height + ((start_minute - start_absolute) / 60) * hour_height + 3
                y1 = header_height + ((end_minute - start_absolute) / 60) * hour_height - 3
                y1 = max(y0 + 23, y1)

                subject = str(lesson.get("SubjectName") or "Lesson")
                teacher = str(lesson.get("TeacherName") or "")
                room = str(lesson.get("RoomName") or "")
                start_label = f"{start_minute // 60:02d}:{start_minute % 60:02d}"
                end_label = f"{(end_minute % (24 * 60)) // 60:02d}:{end_minute % 60:02d}"
                time_label = f"{start_label} – {end_label}"
                details = "  ·  ".join(part for part in (teacher, room) if part and part != "-")
                color_index = sum(ord(char) for char in subject.casefold()) % len(lesson_colors)
                fill, outline = lesson_colors[color_index]
                self._rounded_rectangle(
                    canvas,
                    x0,
                    y0,
                    x1,
                    y1,
                    8,
                    fill=fill,
                    outline=outline,
                    width=1,
                )

                compact = lane_width < 108
                label = subject if not compact else subject[:13]
                label += f"\n{time_label}"
                if details and y1 - y0 >= 51 and not compact:
                    label += f"\n{details}"
                elif details and y1 - y0 >= 66 and compact:
                    label += f"\n{details[:17]}"
                canvas.create_text(
                    x0 + 8,
                    y0 + 7,
                    anchor="nw",
                    width=max(24, x1 - x0 - 16),
                    text=label,
                    fill=palette["lesson_text"],
                    font=("Segoe UI", 8, "bold"),
                    justify="left",
                )

        now = dt.datetime.now()
        today = now.date()
        if week_start <= today <= week_start + dt.timedelta(days=6):
            current_minute = now.hour * 60 + now.minute
            if start_absolute <= current_minute <= end_absolute:
                y = header_height + ((current_minute - start_absolute) / 60) * hour_height
                canvas.create_line(
                    time_col_width,
                    y,
                    total_width,
                    y,
                    fill=palette["accent"],
                    width=3,
                    capstyle="round",
                )
                canvas.create_oval(
                    time_col_width - 6,
                    y - 6,
                    time_col_width + 6,
                    y + 6,
                    fill=palette["accent"],
                    outline=palette["accent"],
                    width=1,
                )
                canvas.create_text(
                    time_col_width + 10,
                    y - 12,
                    text=now.strftime("%H:%M"),
                    anchor="w",
                    fill=palette["accent"],
                    font=("Segoe UI", 8, "bold"),
                )

        canvas.configure(scrollregion=(0, 0, total_width, total_height))

    def _update_week_label(self, *_args: Any) -> None:
        try:
            reference = dt.datetime.strptime(self.date_var.get().strip(), "%Y-%m-%d").date()
            monday, sunday = week_bounds(reference)
            self.week_range_var.set(f"{monday:%d %b} – {sunday:%d %b %Y}")
        except ValueError:
            self.week_range_var.set("Enter date as YYYY-MM-DD")

    def _shift_week(self, days: int) -> None:
        """Move the selected week and refresh 250 ms after the last arrow click."""
        try:
            reference = dt.datetime.strptime(self.date_var.get().strip(), "%Y-%m-%d").date()
        except ValueError:
            reference = dt.date.today()

        self.date_var.set((reference + dt.timedelta(days=days)).isoformat())

        # Debounce rapid clicks so a burst of navigation causes only one API fetch.
        if self._week_refresh_job is not None:
            try:
                self.root.after_cancel(self._week_refresh_job)
            except tk.TclError:
                pass
        self._week_refresh_job = self.root.after(250, self._auto_refresh_week)

    def _auto_refresh_week(self) -> None:
        self._week_refresh_job = None
        if self.busy:
            # If a request is already running, fetch the latest selected week
            # immediately after that request has completed.
            self._refresh_after_busy = True
            return
        self._start_fetch()

    def _open_data_folder(self) -> None:
        DATA_FOLDER.mkdir(parents=True, exist_ok=True)
        path = str(DATA_FOLDER.resolve())
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except (OSError, AttributeError) as exc:
            messagebox.showerror("Could not open folder", str(exc))


def main() -> None:
    app = ctk.CTk()
    ScheduleApp(app)
    app.mainloop()


if __name__ == "__main__":
    main()