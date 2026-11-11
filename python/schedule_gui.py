"""Louisenlund weekly timetable with a modern CustomTkinter interface.

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

SCHOOL_ID = 35
REQUEST_TIMEOUT = 15
TEMP_FOLDER = Path("temp")
DATA_FOLDER = Path("data")


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
        lessons = [lesson for lesson in lessons if lesson.get("SubjectRelevantAbsences")]

    fields = ("TeacherName", "SubjectName", "RoomName", "start", "end")
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
        self.last_output_path: Path | None = None
        self.current_lessons: list[dict[str, Any]] = []
        self.current_week_start: dt.date | None = None
        self._grid_resize_job: str | None = None
        self._week_refresh_job: str | None = None
        self._refresh_after_busy = False
        self.colors = self.LIGHT

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
            text="WEEKLY TIMETABLE  ·  YOUR SCHOOL WEEK AT A GLANCE",
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

        # Persistent week navigation: stays visible even when Account & settings is closed.
        week_toolbar = ctk.CTkFrame(page, fg_color="transparent")
        week_toolbar.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        # Keep the previous/range/next controls grouped at the left edge.

        self.prev_button = ctk.CTkButton(
            week_toolbar,
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
            week_toolbar,
            textvariable=self.week_range_var,
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=("#53627A", "#B0BED2"),
            anchor="w",
        ).grid(row=0, column=1, sticky="w", padx=(4, 10))

        self.next_button = ctk.CTkButton(
            week_toolbar,
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
        self.status_var.set(f"Preparing week {week_start:%d %b}–{week_end:%d %b}…")
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
        except queue.Empty:
            pass
        self.root.after(120, self._poll_queue)

    def _show_result(self, result: dict[str, Any]) -> None:
        self._finish_busy()
        self.current_lessons = sorted(
            result["lessons"], key=lambda lesson: str(lesson.get("start", ""))
        )
        self.current_week_start = result["week_start"]
        self._draw_week_grid()
        week_start: dt.date = result["week_start"]
        week_end: dt.date = result["week_end"]
        self.last_output_path = Path(result["output_path"])
        self.status_var.set(
            f"Saved {week_start:%d %b}–{week_end:%d %b %Y}  ·  {result['output_path']}"
        )
        self.count_var.set(f"{len(self.current_lessons)} lessons · 7 days")

    def _finish_busy(self) -> None:
        self.busy = False
        self.fetch_button.configure(state="normal", text="↻   Fetch whole week")
        self.progress.stop()
        self.progress.set(0)
        if self._refresh_after_busy:
            self._refresh_after_busy = False
            self.root.after(0, self._start_fetch)

    def _on_grid_resize(self, _event: tk.Event) -> None:
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
