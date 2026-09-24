"""Read-only Canvas LMS access for the authenticated student's submissions.

Uses GET only. Never submits, grades, excuses, or otherwise writes.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

HTTP_TIMEOUT_SECONDS = 15.0
MAX_PAGES = 20
PER_PAGE = 100
DUE_SOON_SECONDS = 48 * 3600
MAX_DAYS_AHEAD = 366
ID_RE = re.compile(r"^[1-9][0-9]{0,18}$")

# Canvas Assignment.submission_types documented values that do not collect a
# Canvas-hosted student submission. See Canvas Assignment object +
# AbstractAssignment#expects_submission? / #expects_external_submission?
NO_CANVAS_SUBMISSION_TYPES = frozenset({"none", "not_graded", "wiki_page"})
ON_PAPER_TYPE = "on_paper"
EXTERNAL_TOOL_TYPE = "external_tool"

# Canvas Submission.workflow_state documented values that mean the student
# has already handed the work in (possibly still waiting on a grade).
COMPLETED_WORKFLOW_STATES = frozenset({"submitted", "graded", "pending_review"})


def _error(code: str, message: str) -> dict:
    return {"ok": False, "error": message, "code": code}


def _base_url() -> str:
    return os.environ.get("CANVAS_BASE_URL", "").strip().rstrip("/")


def _token() -> str:
    return os.environ.get("CANVAS_ACCESS_TOKEN", "").strip()


def _api_root() -> str:
    base = _base_url()
    if base.endswith("/api/v1"):
        return base
    return f"{base}/api/v1"


def _validate_id(value: Any, field: str) -> str | dict:
    item = str(value or "").strip()
    if not item or not ID_RE.fullmatch(item):
        return _error("INVALID_ID", f"{field} is not a valid Canvas id")
    return item


def _auth_config_error() -> dict | None:
    if not _base_url() or not _token():
        return _error(
            "AUTH_NOT_CONFIGURED",
            "Set CANVAS_BASE_URL and CANVAS_ACCESS_TOKEN on the MCP process "
            "(Canvas personal access token). Do not commit the token.",
        )
    parsed = urlparse(_base_url())
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        return _error("AUTH_NOT_CONFIGURED", "CANVAS_BASE_URL must be an http(s) origin")
    return None


def _allowed_url(url: str) -> bool:
    parsed = urlparse(url)
    base = urlparse(_base_url())
    return parsed.scheme in {"https", "http"} and parsed.netloc == base.netloc


def _next_page(response: httpx.Response) -> str | None:
    link = response.headers.get("Link") or response.headers.get("link") or ""
    for part in link.split(","):
        if "rel=\"next\"" not in part and "rel='next'" not in part and "rel=next" not in part:
            continue
        start = part.find("<")
        end = part.find(">", start + 1)
        if start < 0 or end < 0:
            continue
        url = part[start + 1 : end]
        return url if _allowed_url(url) else None
    return None


def _get(path: str, params: list[tuple[str, str]] | None = None) -> dict | list | httpx.Response:
    cfg = _auth_config_error()
    if cfg:
        return cfg
    url = path if path.startswith("http") else f"{_api_root()}{path}"
    if not _allowed_url(url):
        return _error("INVALID_URL", "Refusing to request a host other than CANVAS_BASE_URL")
    headers = {"Authorization": f"Bearer {_token()}", "Accept": "application/json"}
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = client.get(url, params=params, headers=headers)
    except httpx.TimeoutException:
        return _error("CANVAS_TIMEOUT", "Canvas request timed out")
    except httpx.HTTPError as exc:
        return _error("CANVAS_UNAVAILABLE", f"Could not reach Canvas: {exc}")
    if response.status_code == 401:
        return _error("AUTH_FAILED", "Canvas rejected the access token")
    if response.status_code == 403:
        return _error("FORBIDDEN", "Canvas denied access to this resource")
    if response.status_code == 404:
        return _error("NOT_FOUND", "Canvas did not find that course or assignment")
    if response.status_code >= 400:
        return _error("CANVAS_ERROR", f"Canvas returned HTTP {response.status_code}")
    return response


def _json_list(payload: httpx.Response | dict | list) -> list | dict:
    if isinstance(payload, dict) and payload.get("ok") is False:
        return payload
    if not isinstance(payload, httpx.Response):
        return _error("CANVAS_ERROR", "Unexpected Canvas response")
    try:
        data = payload.json()
    except ValueError:
        return _error("CANVAS_ERROR", "Canvas returned non-JSON")
    if not isinstance(data, list):
        return _error("CANVAS_ERROR", "Canvas returned a non-list where a list was required")
    return data


def _json_obj(payload: httpx.Response | dict | list) -> dict:
    if isinstance(payload, dict) and payload.get("ok") is False:
        return payload
    if not isinstance(payload, httpx.Response):
        return _error("CANVAS_ERROR", "Unexpected Canvas response")
    try:
        data = payload.json()
    except ValueError:
        return _error("CANVAS_ERROR", "Canvas returned non-JSON")
    if not isinstance(data, dict):
        return _error("CANVAS_ERROR", "Canvas returned a non-object where an object was required")
    return data


def _paginate(path: str, params: list[tuple[str, str]]) -> list | dict:
    items: list = []
    url: str | None = None
    query: list[tuple[str, str]] | None = params
    for _ in range(MAX_PAGES):
        payload = _get(url or path, query)
        page = _json_list(payload)
        if isinstance(page, dict):
            return page
        items.extend(page)
        if not isinstance(payload, httpx.Response):
            break
        url = _next_page(payload)
        query = None
        if not url:
            break
    return items


def _parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _due_in_text(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    overdue = seconds < 0
    remaining = abs(int(seconds))
    days, rem = divmod(remaining, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts: list[str] = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if not parts and minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if not parts:
        parts.append("less than a minute")
    label = " ".join(parts)
    return f"overdue by {label}" if overdue else f"in {label}"


def _submission_types(assignment: dict) -> list[str]:
    raw = assignment.get("submission_types")
    if isinstance(raw, list):
        return [str(item) for item in raw if item]
    if isinstance(raw, str) and raw.strip():
        return [part.strip() for part in raw.split(",") if part.strip()]
    return []


def _submission(assignment: dict) -> dict | None:
    raw = assignment.get("submission")
    return raw if isinstance(raw, dict) else None


def _requires_canvas_submission(assignment: dict) -> bool:
    """True when Canvas collects a student submission for this assignment.

    Mirrors Canvas AbstractAssignment#expects_submission?: not none / not_graded
    / wiki_page / on_paper / external_tool. New Quizzes set quiz_lti and still
    record a Canvas submission, so those are treated as requiring one.
    """
    types = _submission_types(assignment)
    if not types or set(types) <= NO_CANVAS_SUBMISSION_TYPES:
        return False
    if types == [ON_PAPER_TYPE]:
        return False
    if types == [EXTERNAL_TOOL_TYPE]:
        return assignment.get("quiz_lti") is True or assignment.get("is_quiz_assignment") is True
    return True


def _is_completed(submission: dict | None) -> bool:
    if not isinstance(submission, dict):
        return False
    if submission.get("redo_request") is True:
        return False
    if submission.get("excused") is True:
        return True
    state = submission.get("workflow_state")
    if isinstance(state, str) and state in COMPLETED_WORKFLOW_STATES:
        return True
    if submission.get("submitted_at"):
        return True
    return False


def _is_incomplete(assignment: dict, submission: dict | None) -> bool:
    if isinstance(submission, dict) and submission.get("redo_request") is True:
        return True
    if _is_completed(submission):
        return False
    if isinstance(submission, dict) and submission.get("assignment_visible") is False:
        return False
    if isinstance(submission, dict) and submission.get("missing") is True:
        return True
    if _requires_canvas_submission(assignment):
        if not isinstance(submission, dict):
            return True
        state = submission.get("workflow_state")
        if state == "unsubmitted" or not submission.get("submitted_at"):
            return True
    return False


def _completion_state(assignment: dict, submission: dict | None) -> str:
    types = _submission_types(assignment)
    if isinstance(submission, dict):
        if submission.get("redo_request") is True:
            return "redo_requested"
        if submission.get("excused") is True:
            return "excused"
        state = submission.get("workflow_state")
        if state == "graded":
            return "graded"
        if state == "pending_review":
            return "submitted_not_graded"
        if state == "submitted" or submission.get("submitted_at"):
            return "submitted_not_graded"
        if submission.get("missing") is True:
            return "missing"
        if state == "unsubmitted":
            if types == [EXTERNAL_TOOL_TYPE] and not _requires_canvas_submission(assignment):
                return "external_tool_unconfirmed"
            if not _requires_canvas_submission(assignment):
                return "no_submission_required"
            return "unsubmitted"
    if types == [EXTERNAL_TOOL_TYPE] and not _requires_canvas_submission(assignment):
        return "external_tool_unconfirmed"
    if not _requires_canvas_submission(assignment):
        return "no_submission_required"
    return "unsubmitted"


def _due_class(due_at: datetime | None, now: datetime) -> str:
    if due_at is None:
        return "no_due_date"
    if due_at < now:
        return "overdue"
    if (due_at - now).total_seconds() <= DUE_SOON_SECONDS:
        return "due_soon"
    return "upcoming"


def _copy_canvas_flag(target: dict, source: dict | None, key: str) -> None:
    if isinstance(source, dict) and key in source:
        target[key] = source.get(key)


def _published(assignment: dict) -> bool:
    if "published" in assignment:
        return assignment.get("published") is True
    return assignment.get("workflow_state") != "unpublished"


def _current_course(course: dict) -> bool:
    if not isinstance(course, dict) or course.get("id") is None:
        return False
    if course.get("access_restricted_by_date") is True:
        return False
    if course.get("concluded") is True:
        return False
    state = course.get("workflow_state")
    if state in {"completed", "deleted", "unpublished"}:
        return False
    return True


def _list_current_courses() -> list | dict:
    rows = _paginate(
        "/courses",
        [
            ("enrollment_state", "active"),
            ("enrollment_type", "student"),
            ("include[]", "concluded"),
            ("per_page", str(PER_PAGE)),
        ],
    )
    if isinstance(rows, dict):
        return rows
    return [course for course in rows if _current_course(course)]


def _list_course_assignments(course_id: str) -> list | dict:
    return _paginate(
        f"/courses/{course_id}/assignments",
        [
            ("include[]", "submission"),
            ("per_page", str(PER_PAGE)),
            ("order_by", "due_at"),
        ],
    )


def _summarize_unfinished(
    course: dict,
    assignment: dict,
    now: datetime,
) -> dict | None:
    if not _published(assignment):
        return None
    submission = _submission(assignment)
    if not _is_incomplete(assignment, submission):
        return None

    due_at = _parse_dt(assignment.get("due_at"))
    due_class = _due_class(due_at, now)
    due_seconds = int((due_at - now).total_seconds()) if due_at else None
    state = submission.get("workflow_state") if isinstance(submission, dict) else None

    item = {
        "course_id": course.get("id"),
        "course_name": course.get("name"),
        "course_code": course.get("course_code"),
        "assignment_id": assignment.get("id"),
        "assignment_name": assignment.get("name"),
        "due_at": assignment.get("due_at"),
        "points_possible": assignment.get("points_possible"),
        "submission_types": _submission_types(assignment),
        "submission_status": state if isinstance(state, str) else "unsubmitted",
        "html_url": assignment.get("html_url"),
        "due_class": due_class,
        "due_in": _due_in_text(due_seconds),
        "due_in_seconds": due_seconds,
    }
    _copy_canvas_flag(item, submission, "missing")
    _copy_canvas_flag(item, submission, "late")
    if isinstance(submission, dict) and "submitted_at" in submission:
        item["submitted_at"] = submission.get("submitted_at")
    item["_sort_due"] = due_at.timestamp() if due_at else None
    return item


def _sort_unfinished(items: list[dict]) -> list[dict]:
    def key(item: dict) -> tuple:
        due_class = item.get("due_class")
        due = item.get("_sort_due")
        if due_class == "overdue":
            return (0, due if due is not None else float("-inf"))
        if due is not None:
            return (1, due)
        return (2, float("inf"))

    ordered = sorted(items, key=key)
    for item in ordered:
        item.pop("_sort_due", None)
    return ordered


def _in_horizon(item: dict, include_overdue: bool, include_future: bool, horizon: datetime | None) -> bool:
    due_class = item.get("due_class")
    if due_class == "overdue":
        return include_overdue
    if due_class == "no_due_date":
        return include_future and horizon is None
    if not include_future:
        return False
    if horizon is None:
        return True
    due = _parse_dt(item.get("due_at"))
    return due is not None and due <= horizon


def get_incomplete_canvas_assignments(
    include_overdue: bool = True,
    include_future: bool = True,
    days_ahead: int | None = None,
) -> dict:
    """Return the authenticated user's unfinished Canvas assignments."""
    cfg = _auth_config_error()
    if cfg:
        return cfg
    if days_ahead is not None:
        try:
            days_ahead = int(days_ahead)
        except (TypeError, ValueError):
            return _error("INVALID_DAYS_AHEAD", "days_ahead must be an integer or omitted")
        if days_ahead < 0 or days_ahead > MAX_DAYS_AHEAD:
            return _error("INVALID_DAYS_AHEAD", f"days_ahead must be between 0 and {MAX_DAYS_AHEAD}")

    now = _now()
    horizon = (now + timedelta(days=days_ahead)) if days_ahead is not None else None
    courses = _list_current_courses()
    if isinstance(courses, dict):
        return courses

    unfinished: list[dict] = []
    course_errors: list[dict] = []
    for course in courses:
        course_id = _validate_id(course.get("id"), "course_id")
        if isinstance(course_id, dict):
            continue
        assignments = _list_course_assignments(course_id)
        if isinstance(assignments, dict):
            course_errors.append(
                {
                    "course_id": course.get("id"),
                    "course_name": course.get("name"),
                    "code": assignments.get("code"),
                    "error": assignments.get("error"),
                }
            )
            continue
        for assignment in assignments:
            if not isinstance(assignment, dict):
                continue
            item = _summarize_unfinished(course, assignment, now)
            if item and _in_horizon(item, include_overdue, include_future, horizon):
                unfinished.append(item)

    return {
        "ok": True,
        "count": len(unfinished),
        "include_overdue": bool(include_overdue),
        "include_future": bool(include_future),
        "days_ahead": days_ahead,
        "assignments": _sort_unfinished(unfinished),
        "course_errors": course_errors,
    }


def get_canvas_assignment_status(course_id: str | int, assignment_id: str | int) -> dict:
    """Return the authenticated user's submission status for one assignment."""
    cfg = _auth_config_error()
    if cfg:
        return cfg
    course_key = _validate_id(course_id, "course_id")
    if isinstance(course_key, dict):
        return course_key
    assignment_key = _validate_id(assignment_id, "assignment_id")
    if isinstance(assignment_key, dict):
        return assignment_key

    course_payload = _json_obj(_get(f"/courses/{course_key}"))
    if course_payload.get("ok") is False:
        return course_payload

    assignment_payload = _get(
        f"/courses/{course_key}/assignments/{assignment_key}",
        [("include[]", "submission")],
    )
    assignment = _json_obj(assignment_payload)
    if assignment.get("ok") is False:
        return assignment

    submission = _submission(assignment)
    now = _now()
    due_at = _parse_dt(assignment.get("due_at"))
    due_seconds = int((due_at - now).total_seconds()) if due_at else None
    completed = _is_completed(submission)
    state = submission.get("workflow_state") if isinstance(submission, dict) else None

    result = {
        "ok": True,
        "completed": completed,
        "incomplete": _is_incomplete(assignment, submission),
        "completion_state": _completion_state(assignment, submission),
        "course_id": course_payload.get("id"),
        "course_name": course_payload.get("name"),
        "course_code": course_payload.get("course_code"),
        "assignment_id": assignment.get("id"),
        "assignment_name": assignment.get("name"),
        "due_at": assignment.get("due_at"),
        "points_possible": assignment.get("points_possible"),
        "submission_types": _submission_types(assignment),
        "requires_canvas_submission": _requires_canvas_submission(assignment),
        "submission_status": state if isinstance(state, str) else None,
        "html_url": assignment.get("html_url"),
        "due_class": _due_class(due_at, now),
        "due_in": _due_in_text(due_seconds),
        "due_in_seconds": due_seconds,
    }
    if assignment.get("quiz_lti") is True:
        result["quiz_lti"] = True
    if assignment.get("is_quiz_assignment") is True:
        result["is_quiz_assignment"] = True
    _copy_canvas_flag(result, submission, "missing")
    _copy_canvas_flag(result, submission, "late")
    _copy_canvas_flag(result, submission, "excused")
    _copy_canvas_flag(result, submission, "redo_request")
    if isinstance(submission, dict):
        if "submitted_at" in submission:
            result["submitted_at"] = submission.get("submitted_at")
        if "graded_at" in submission:
            result["graded_at"] = submission.get("graded_at")
        if "score" in submission:
            result["score"] = submission.get("score")
        if "grade" in submission:
            result["grade"] = submission.get("grade")
    return result
