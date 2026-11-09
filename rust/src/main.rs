//! Fetch today's schedule from the school portal, save it to JSON, and print
//! a clean day-view table to the console.
//!
//! Rust port of the original Python script. Behavior mirrors the original:
//! - Auth against the JWT endpoint.
//! - Fetch the cached schedule (sent as form-encoded data, matching the
//!   original's `data=payload` call).
//! - Filter to lessons with a truthy `SubjectRelevantAbsences` field.
//! - Optionally merge back-to-back identical lesson blocks.
//! - Save the simplified schedule to `./data/schedule-YYYY-MM-DD.json` and a
//!   raw debug dump to `./temp/schedule_<timestamp>.json`.
//! - Print a colored day-view table to the console.

use std::error::Error as StdError;
use std::fmt;
use std::fs;

use chrono::{Local, NaiveDate, NaiveDateTime};
use clap::Parser;
use serde::{Deserialize, Serialize};
use serde_json::Value;

// --------------------------------------------------------------------------
// Config
// --------------------------------------------------------------------------

const PROXY_URL: &str = "socks5h://127.0.0.1:1080";
const BASE_URL: &str = "https://schule.louisenlund.de/api";
const SCHOOL_ID: i64 = 35;
const STUDENT_ID: i64 = 6296;
#[allow(dead_code)]
const SEMESTER_ID: i64 = 330;
const TEMP_FOLDER: &str = "./temp";
const DATA_FOLDER: &str = "./data";
const REQUEST_TIMEOUT_SECS: u64 = 15;

fn auth_url() -> String {
    format!("{BASE_URL}//Api/Authorization/JwtAuth")
}

fn schedule_url() -> String {
    format!("{BASE_URL}//api/Schedule/GetScheduleCached")
}

// --------------------------------------------------------------------------
// Colors / logging
// --------------------------------------------------------------------------

mod colors {
    pub const RED: &str = "\x1b[91m";
    pub const GREEN: &str = "\x1b[92m";
    pub const YELLOW: &str = "\x1b[93m";
    pub const BLUE: &str = "\x1b[94m";
    pub const RESET: &str = "\x1b[0m";
}

fn log(msg: &str, color: &str, ok: bool) {
    let mark = if ok { "v]" } else { "x]" };
    let now = Local::now().format("%Y-%m-%d %H:%M:%S");
    println!("{color}{now}    {mark} {msg}{}", colors::RESET);
}

fn log_info(msg: &str) {
    log(msg, colors::BLUE, true);
}

fn log_err(msg: &str) {
    log(msg, colors::RED, false);
}

// --------------------------------------------------------------------------
// Errors
// --------------------------------------------------------------------------

/// Mirrors `ScheduleError` in the original script: raised for any expected
/// failure, with a message already meant for the user.
#[derive(Debug)]
struct ScheduleError(String);

impl fmt::Display for ScheduleError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl StdError for ScheduleError {}

impl ScheduleError {
    fn new(msg: impl Into<String>) -> Self {
        ScheduleError(msg.into())
    }
}

fn truncate(s: &str, n: usize) -> String {
    if s.chars().count() <= n {
        s.to_string()
    } else {
        s.chars().take(n).collect()
    }
}

// --------------------------------------------------------------------------
// HTTP helpers
// --------------------------------------------------------------------------

fn build_client() -> Result<reqwest::blocking::Client, ScheduleError> {
    let proxy = reqwest::Proxy::all(PROXY_URL)
        .map_err(|exc| ScheduleError::new(format!("Invalid proxy URL {PROXY_URL}: {exc}")))?;

    reqwest::blocking::Client::builder()
        .proxy(proxy)
        .danger_accept_invalid_certs(true)
        .timeout(std::time::Duration::from_secs(REQUEST_TIMEOUT_SECS))
        .build()
        .map_err(|exc| ScheduleError::new(format!("Couldn't build HTTP client: {exc}")))
}

/// Classify a reqwest error the same way the Python version distinguishes
/// proxy / connection / timeout failures.
fn describe_transport_error(url: &str, exc: &reqwest::Error) -> ScheduleError {
    if exc.is_timeout() {
        ScheduleError::new(format!(
            "Request to {url} timed out after {REQUEST_TIMEOUT_SECS}s."
        ))
    } else if exc.is_connect() {
        // reqwest doesn't distinguish "proxy unreachable" from "target
        // unreachable" as cleanly as urllib3/requests does, so we fold both
        // into one connection-error message but keep the proxy hint.
        ScheduleError::new(format!(
            "Couldn't connect to {url} (via proxy {PROXY_URL}): {exc}"
        ))
    } else {
        ScheduleError::new(format!("Request to {url} failed: {exc}"))
    }
}

fn get_token(
    client: &reqwest::blocking::Client,
    url: &str,
    username: Option<&str>,
    password: Option<&str>,
) -> Result<String, ScheduleError> {
    let (username, password) = match (username, password) {
        (Some(u), Some(p)) if !u.is_empty() && !p.is_empty() => (u, p),
        _ => {
            return Err(ScheduleError::new(
                "SCHOOL_USERNAME / SCHOOL_PASSWORD are not set. \
                 Export them or put them in a .env before running.",
            ))
        }
    };

    let payload = serde_json::json!({
        "Username": username,
        "Password": password,
        "Pin": "null",
    });

    let resp = client
        .post(url)
        .json(&payload)
        .send()
        .map_err(|exc| describe_transport_error(url, &exc))?;

    let status = resp.status();
    let body_text = resp.text().unwrap_or_default();

    if status.as_u16() == 401 || status.as_u16() == 403 {
        return Err(ScheduleError::new(format!(
            "Login rejected ({}). Username or password is wrong (or the account is locked). \
             Server said: {:?}",
            status.as_u16(),
            truncate(body_text.trim(), 200)
        )));
    }
    if !status.is_success() {
        return Err(ScheduleError::new(format!(
            "Login failed: HTTP {}. Body: {:?}",
            status.as_u16(),
            truncate(body_text.trim(), 200)
        )));
    }

    let token = body_text.trim().trim_matches('"').trim_matches('\'').trim();
    if token.is_empty() {
        return Err(ScheduleError::new(format!(
            "Login succeeded (HTTP {}) but returned an empty token.",
            status.as_u16()
        )));
    }
    Ok(token.to_string())
}

/// POST a form-encoded body and return parsed JSON, raising `ScheduleError`
/// with context on failure. Mirrors `fetch_json` in the original, including
/// the fallback that unwraps a double-encoded JSON string.
fn fetch_json_form(
    client: &reqwest::blocking::Client,
    token: &str,
    url: &str,
    form: &[(&str, String)],
) -> Result<Value, ScheduleError> {
    let resp = client
        .post(url)
        .bearer_auth(token)
        .form(form)
        .send()
        .map_err(|exc| describe_transport_error(url, &exc))?;

    let status = resp.status();
    let body_text = resp.text().unwrap_or_default();

    if status.as_u16() == 401 || status.as_u16() == 403 {
        return Err(ScheduleError::new(format!(
            "{url} rejected the request ({}) — token may have expired or lacks permission. \
             Body: {:?}",
            status.as_u16(),
            truncate(body_text.trim(), 200)
        )));
    }
    if !status.is_success() {
        return Err(ScheduleError::new(format!(
            "{url} returned an error: HTTP {}. Body: {:?}",
            status.as_u16(),
            truncate(body_text.trim(), 200)
        )));
    }

    let mut data: Value = serde_json::from_str(&body_text).map_err(|_| {
        ScheduleError::new(format!(
            "{url} did not return valid JSON (got {:?})",
            truncate(body_text.trim(), 200)
        ))
    })?;

    // Some endpoints on this API double-encode JSON as a string.
    if let Value::String(inner) = &data {
        data = serde_json::from_str(inner).map_err(|exc| {
            ScheduleError::new(format!(
                "{url} returned a string that wasn't valid JSON either: {exc}"
            ))
        })?;
    }

    Ok(data)
}

// --------------------------------------------------------------------------
// Domain logic
// --------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
struct SimplifiedLesson {
    #[serde(rename = "TeacherName")]
    teacher_name: Option<String>,
    #[serde(rename = "SubjectName")]
    subject_name: Option<String>,
    #[serde(rename = "RoomName")]
    room_name: Option<String>,
    start: String,
    end: String,
}

/// Python truthiness for a JSON value: `null`/`false`/`0`/empty
/// string/empty array/empty object are all falsy; everything else is truthy.
fn is_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().map(|f| f != 0.0).unwrap_or(true),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

fn value_to_opt_string(value: Option<&Value>) -> Option<String> {
    match value {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) => Some(s.clone()),
        Some(other) => Some(other.to_string()),
    }
}

fn extract_simplified_lessons(lessons: &[Value]) -> Vec<SimplifiedLesson> {
    lessons
        .iter()
        .filter(|entry| {
            entry
                .get("SubjectRelevantAbsences")
                .map(is_truthy)
                .unwrap_or(false)
        })
        .map(|entry| SimplifiedLesson {
            teacher_name: value_to_opt_string(entry.get("TeacherName")),
            subject_name: value_to_opt_string(entry.get("SubjectName")),
            room_name: value_to_opt_string(entry.get("RoomName")),
            start: entry
                .get("start")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string(),
            end: entry
                .get("end")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string(),
        })
        .collect()
}

/// Merge back-to-back slots that share teacher/subject/room and are
/// contiguous in time (e.g. two 30-min blocks -> one 60-min block).
fn merge_consecutive(lessons: &[SimplifiedLesson]) -> Vec<SimplifiedLesson> {
    let mut sorted: Vec<SimplifiedLesson> = lessons.to_vec();
    sorted.sort_by(|a, b| a.start.cmp(&b.start));

    let mut merged: Vec<SimplifiedLesson> = Vec::new();
    for lesson in sorted {
        if let Some(prev) = merged.last_mut() {
            let same_class = prev.teacher_name == lesson.teacher_name
                && prev.subject_name == lesson.subject_name
                && prev.room_name == lesson.room_name;
            let contiguous = prev.end == lesson.start;
            if same_class && contiguous {
                prev.end = lesson.end;
                continue;
            }
        }
        merged.push(lesson);
    }
    merged
}

/// Parse an ISO-8601-ish datetime string the way Python's
/// `datetime.fromisoformat` would for the formats this API returns.
fn parse_datetime(s: &str) -> Option<NaiveDateTime> {
    if s.is_empty() {
        return None;
    }
    // Try RFC3339 / ISO8601 with offset or "Z" first.
    if let Ok(dt) = chrono::DateTime::parse_from_rfc3339(s) {
        return Some(dt.naive_local());
    }
    // Fall back to a plain "YYYY-MM-DDTHH:MM:SS[.fff]" with no offset.
    for fmt in ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%dT%H:%M:%S"] {
        if let Ok(dt) = NaiveDateTime::parse_from_str(s, fmt) {
            return Some(dt);
        }
    }
    None
}

fn center(text: &str, width: usize, fill: char) -> String {
    let len = text.chars().count();
    if len >= width {
        return text.to_string();
    }
    let total_pad = width - len;
    let left = total_pad / 2;
    let right = total_pad - left;
    format!(
        "{}{}{}",
        fill.to_string().repeat(left),
        text,
        fill.to_string().repeat(right)
    )
}

fn pad_right(text: &str, width: usize) -> String {
    let len = text.chars().count();
    if len >= width {
        text.to_string()
    } else {
        format!("{}{}", text, " ".repeat(width - len))
    }
}

/// Pretty-print the day's schedule to the console.
fn print_day_schedule(lessons: &[SimplifiedLesson], date: NaiveDate) {
    let mut day_lessons: Vec<&SimplifiedLesson> = lessons
        .iter()
        .filter(|l| {
            parse_datetime(&l.start)
                .map(|dt| dt.date() == date)
                .unwrap_or(false)
        })
        .collect();
    day_lessons.sort_by(|a, b| a.start.cmp(&b.start));

    let header = format!(" Schedule for {} ", date.format("%A, %d %B %Y"));
    println!(
        "\n{}{}{}",
        colors::BLUE,
        center(&header, 60, '='),
        colors::RESET
    );

    if day_lessons.is_empty() {
        println!(
            "{}No lessons found for this day.{}\n",
            colors::YELLOW,
            colors::RESET
        );
        return;
    }

    for lesson in day_lessons {
        let start = parse_datetime(&lesson.start)
            .map(|d| d.format("%H:%M").to_string())
            .unwrap_or_else(|| "??:??".to_string());
        let end = parse_datetime(&lesson.end)
            .map(|d| d.format("%H:%M").to_string())
            .unwrap_or_else(|| "??:??".to_string());
        let subject = lesson.subject_name.as_deref().unwrap_or("-");
        let teacher = lesson.teacher_name.as_deref().unwrap_or("-");
        let room = lesson.room_name.as_deref().unwrap_or("-");
        println!(
            "{}{start}-{end}{}  {} {} {room}",
            colors::GREEN,
            colors::RESET,
            pad_right(subject, 20),
            pad_right(teacher, 20),
        );
    }
    println!("{}{}{}\n", colors::BLUE, "=".repeat(60), colors::RESET);
}

// --------------------------------------------------------------------------
// CLI
// --------------------------------------------------------------------------

#[derive(Parser, Debug)]
#[command(about = "Fetch and display school schedule.")]
struct Args {
    /// Date to fetch/display (YYYY-MM-DD). Defaults to today.
    #[arg(long, value_parser = parse_date_arg)]
    date: Option<NaiveDate>,

    /// Don't merge consecutive identical lesson slots.
    #[arg(long)]
    no_merge: bool,
}

fn parse_date_arg(s: &str) -> Result<NaiveDate, String> {
    NaiveDate::parse_from_str(s, "%Y-%m-%d")
        .map_err(|_| format!("invalid date {s:?}, expected YYYY-MM-DD"))
}

// --------------------------------------------------------------------------
// Main
// --------------------------------------------------------------------------

fn run() -> Result<i32, Box<dyn StdError>> {
    let args = Args::parse();
    let date = args.date.unwrap_or_else(|| Local::now().date_naive());

    fs::create_dir_all(TEMP_FOLDER)?;
    fs::create_dir_all(DATA_FOLDER)?;
    let output_path = format!("{DATA_FOLDER}/schedule-{}.json", date.format("%Y-%m-%d"));

    let username = std::env::var("SCHOOL_USERNAME").ok();
    let password = std::env::var("SCHOOL_PASSWORD").ok();

    let client = build_client()?;

    let parsed_schedule = match (|| -> Result<Value, ScheduleError> {
        log_info("Getting auth token...");
        let token = get_token(&client, &auth_url(), username.as_deref(), password.as_deref())?;

        // Note: mirrors the original's `data=payload` call, which `requests`
        // sends as application/x-www-form-urlencoded (not JSON), and
        // stringifies each value the same way Python's `str()` would.
        let from = format!("{date}T01:00:00.000Z");
        let to = format!("{date}T23:00:00.000Z");
        let form: Vec<(&str, String)> = vec![
            ("schoolId", SCHOOL_ID.to_string()),
            ("from", from),
            ("to", to),
            ("studentId", STUDENT_ID.to_string()),
            ("getShortNames", "False".to_string()),
            ("getAbsences", "False".to_string()),
        ];

        log_info("Getting schedule...");
        fetch_json_form(&client, &token, &schedule_url(), &form)
    })() {
        Ok(v) => v,
        Err(exc) => {
            log_err(&exc.to_string());
            return Ok(1);
        }
    };

    // Raw dump for debugging.
    let timestamp = Local::now().format("%Y%m%dT%H%M%S");
    let raw_path = format!("{TEMP_FOLDER}/schedule_{timestamp}.json");
    fs::write(&raw_path, serde_json::to_string_pretty(&parsed_schedule)?)?;

    let lessons: Vec<Value> = parsed_schedule
        .get("lessons")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();

    let mut simplified = extract_simplified_lessons(&lessons);
    if !args.no_merge {
        simplified = merge_consecutive(&simplified);
    }

    fs::write(&output_path, serde_json::to_string_pretty(&simplified)?)?;

    log_info(&format!("Finished! Saved to {output_path}"));
    print_day_schedule(&simplified, date);

    Ok(0)
}

fn main() {
    // Unexpected/programming errors show the real error instead of hiding
    // it, mirroring the original's re-raise of unanticipated exceptions.
    match run() {
        Ok(code) => std::process::exit(code),
        Err(exc) => {
            log_err(&format!("Unexpected error: {exc:?}"));
            std::process::exit(1);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn merges_contiguous_identical_lessons() {
        let lessons = vec![
            SimplifiedLesson {
                teacher_name: Some("Smith".into()),
                subject_name: Some("Math".into()),
                room_name: Some("101".into()),
                start: "2024-01-15T08:00:00".into(),
                end: "2024-01-15T08:30:00".into(),
            },
            SimplifiedLesson {
                teacher_name: Some("Smith".into()),
                subject_name: Some("Math".into()),
                room_name: Some("101".into()),
                start: "2024-01-15T08:30:00".into(),
                end: "2024-01-15T09:00:00".into(),
            },
            SimplifiedLesson {
                teacher_name: Some("Jones".into()),
                subject_name: Some("Art".into()),
                room_name: Some("102".into()),
                start: "2024-01-15T09:00:00".into(),
                end: "2024-01-15T09:30:00".into(),
            },
        ];
        let merged = merge_consecutive(&lessons);
        assert_eq!(merged.len(), 2);
        assert_eq!(merged[0].start, "2024-01-15T08:00:00");
        assert_eq!(merged[0].end, "2024-01-15T09:00:00");
        assert_eq!(merged[1].subject_name.as_deref(), Some("Art"));
    }

    #[test]
    fn truthy_matches_python_semantics() {
        assert!(!is_truthy(&Value::Null));
        assert!(!is_truthy(&Value::Bool(false)));
        assert!(!is_truthy(&serde_json::json!(0)));
        assert!(!is_truthy(&serde_json::json!("")));
        assert!(is_truthy(&serde_json::json!("x")));
        assert!(is_truthy(&serde_json::json!(1)));
        assert!(is_truthy(&Value::Bool(true)));
    }

    #[test]
    fn extract_filters_by_subject_relevant_absences() {
        let raw = serde_json::json!([
            {"TeacherName": "A", "SubjectName": "Math", "RoomName": "1", "start": "s1", "end": "e1", "SubjectRelevantAbsences": true},
            {"TeacherName": "B", "SubjectName": "Art", "RoomName": "2", "start": "s2", "end": "e2", "SubjectRelevantAbsences": false},
            {"TeacherName": "C", "SubjectName": "PE", "RoomName": "3", "start": "s3", "end": "e3"}
        ]);
        let arr: Vec<Value> = raw.as_array().unwrap().clone();
        let simplified = extract_simplified_lessons(&arr);
        assert_eq!(simplified.len(), 1);
        assert_eq!(simplified[0].subject_name.as_deref(), Some("Math"));
    }

    #[test]
    fn parses_various_datetime_formats() {
        assert!(parse_datetime("2024-01-15T08:00:00").is_some());
        assert!(parse_datetime("2024-01-15T08:00:00Z").is_some());
        assert!(parse_datetime("2024-01-15T08:00:00+01:00").is_some());
        assert!(parse_datetime("").is_none());
    }
}