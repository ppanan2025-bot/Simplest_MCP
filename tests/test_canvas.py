from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx

import canvas


def _response(body, status_code=200, link=""):
    return httpx.Response(
        status_code,
        json=body,
        headers={"Link": link} if link else {},
        request=httpx.Request("GET", "https://canvas.example.test/api/v1/courses"),
    )


class CanvasLogicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 9, 24, 1, 0, tzinfo=timezone.utc)
        self.now_patch = mock.patch.object(canvas, "_now", return_value=self.now)
        self.now_patch.start()
        self.env = mock.patch.dict(
            "os.environ",
            {
                "CANVAS_BASE_URL": "https://canvas.example.test",
                "CANVAS_ACCESS_TOKEN": "test-token",
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self) -> None:
        self.now_patch.stop()
        self.env.stop()

    def test_not_configured(self) -> None:
        with mock.patch.dict("os.environ", {"CANVAS_BASE_URL": "", "CANVAS_ACCESS_TOKEN": ""}, clear=False):
            result = canvas.get_incomplete_canvas_assignments()
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "AUTH_NOT_CONFIGURED")
        dumped = json.dumps(result)
        self.assertNotIn("test-token", dumped)
        self.assertNotIn("Bearer ", dumped)

    def test_invalid_ids(self) -> None:
        bad = canvas.get_canvas_assignment_status("../etc/passwd", "1")
        self.assertEqual(bad["code"], "INVALID_ID")
        bad_asg = canvas.get_canvas_assignment_status("12", "id; curl")
        self.assertEqual(bad_asg["code"], "INVALID_ID")

    def test_none_submission_type_is_not_unfinished(self) -> None:
        assignment = {"id": 1, "name": "Syllabus", "published": True, "submission_types": ["none"]}
        self.assertFalse(canvas._requires_canvas_submission(assignment))
        self.assertFalse(canvas._is_incomplete(assignment, {"workflow_state": "unsubmitted"}))

    def test_on_paper_not_unfinished_without_missing(self) -> None:
        assignment = {"id": 2, "name": "Exam", "published": True, "submission_types": ["on_paper"]}
        self.assertFalse(canvas._is_incomplete(assignment, {"workflow_state": "unsubmitted"}))
        self.assertTrue(canvas._is_incomplete(assignment, {"workflow_state": "unsubmitted", "missing": True}))

    def test_external_tool_unconfirmed_without_missing(self) -> None:
        assignment = {"id": 3, "name": "LTI", "published": True, "submission_types": ["external_tool"]}
        self.assertFalse(canvas._requires_canvas_submission(assignment))
        self.assertFalse(canvas._is_incomplete(assignment, {"workflow_state": "unsubmitted"}))
        self.assertEqual(canvas._completion_state(assignment, {"workflow_state": "unsubmitted"}), "external_tool_unconfirmed")

    def test_external_tool_missing_is_unfinished(self) -> None:
        assignment = {"id": 4, "name": "LTI", "published": True, "submission_types": ["external_tool"]}
        self.assertTrue(canvas._is_incomplete(assignment, {"workflow_state": "unsubmitted", "missing": True}))

    def test_new_quiz_without_submit_is_unfinished(self) -> None:
        assignment = {
            "id": 5,
            "name": "New Quiz",
            "published": True,
            "submission_types": ["external_tool"],
            "quiz_lti": True,
        }
        self.assertTrue(canvas._requires_canvas_submission(assignment))
        self.assertTrue(canvas._is_incomplete(assignment, {"workflow_state": "unsubmitted", "submitted_at": None}))

    def test_submitted_without_grade_is_complete(self) -> None:
        submission = {"workflow_state": "submitted", "submitted_at": "2026-09-20T01:00:00Z", "score": None}
        self.assertTrue(canvas._is_completed(submission))
        self.assertFalse(canvas._is_incomplete({"submission_types": ["online_upload"]}, submission))
        self.assertEqual(canvas._completion_state({"submission_types": ["online_upload"]}, submission), "submitted_not_graded")

    def test_graded_and_excused_are_complete(self) -> None:
        self.assertTrue(canvas._is_completed({"workflow_state": "graded", "score": 10}))
        self.assertTrue(canvas._is_completed({"workflow_state": "unsubmitted", "excused": True}))
        self.assertTrue(canvas._is_completed({"workflow_state": "pending_review"}))

    def test_quiz_unsubmitted_is_unfinished(self) -> None:
        assignment = {"submission_types": ["online_quiz"], "is_quiz_assignment": True}
        self.assertTrue(canvas._is_incomplete(assignment, {"workflow_state": "unsubmitted"}))

    def test_absent_submission_unfinished_when_required(self) -> None:
        assignment = {"submission_types": ["online_text_entry"]}
        self.assertTrue(canvas._is_incomplete(assignment, None))

    def test_redo_request_is_unfinished(self) -> None:
        submission = {"workflow_state": "graded", "submitted_at": "2026-09-01T00:00:00Z", "redo_request": True}
        self.assertFalse(canvas._is_completed(submission))
        self.assertTrue(canvas._is_incomplete({"submission_types": ["online_upload"]}, submission))
        self.assertEqual(canvas._completion_state({"submission_types": ["online_upload"]}, submission), "redo_requested")

    def test_sort_overdue_then_nearest_then_undated(self) -> None:
        items = [
            {"assignment_name": "undated", "due_class": "no_due_date", "_sort_due": None},
            {"assignment_name": "later", "due_class": "upcoming", "_sort_due": 300.0},
            {"assignment_name": "soon", "due_class": "due_soon", "_sort_due": 200.0},
            {"assignment_name": "old overdue", "due_class": "overdue", "_sort_due": 10.0},
            {"assignment_name": "recent overdue", "due_class": "overdue", "_sort_due": 50.0},
        ]
        names = [item["assignment_name"] for item in canvas._sort_unfinished(items)]
        self.assertEqual(names, ["old overdue", "recent overdue", "soon", "later", "undated"])

    def test_list_filters_and_omits_guessed_flags(self) -> None:
        overdue_due = (self.now - timedelta(days=2)).isoformat()
        soon_due = (self.now + timedelta(hours=10)).isoformat()
        later_due = (self.now + timedelta(days=10)).isoformat()
        courses = [
            {
                "id": 101,
                "name": "Models of Computation",
                "course_code": "COMP2022",
                "workflow_state": "available",
                "concluded": False,
            }
        ]
        assignments = [
            {
                "id": 1,
                "name": "Done upload",
                "published": True,
                "due_at": overdue_due,
                "points_possible": 10,
                "submission_types": ["online_upload"],
                "html_url": "https://canvas.example.test/courses/101/assignments/1",
                "submission": {"workflow_state": "submitted", "submitted_at": "2026-09-20T00:00:00Z"},
            },
            {
                "id": 2,
                "name": "Late quiz",
                "published": True,
                "due_at": overdue_due,
                "points_possible": 5,
                "submission_types": ["online_quiz"],
                "html_url": "https://canvas.example.test/courses/101/assignments/2",
                "submission": {"workflow_state": "unsubmitted", "missing": True, "late": True, "submitted_at": None},
            },
            {
                "id": 3,
                "name": "Soon essay",
                "published": True,
                "due_at": soon_due,
                "points_possible": 20,
                "submission_types": ["online_text_entry"],
                "html_url": "https://canvas.example.test/courses/101/assignments/3",
                "submission": {"workflow_state": "unsubmitted"},
            },
            {
                "id": 4,
                "name": "Far future",
                "published": True,
                "due_at": later_due,
                "submission_types": ["online_url"],
                "submission": {"workflow_state": "unsubmitted"},
            },
            {
                "id": 5,
                "name": "No submit required",
                "published": True,
                "due_at": soon_due,
                "submission_types": ["none"],
                "submission": {"workflow_state": "unsubmitted"},
            },
            {
                "id": 6,
                "name": "LTI link",
                "published": True,
                "due_at": soon_due,
                "submission_types": ["external_tool"],
                "submission": {"workflow_state": "unsubmitted"},
            },
        ]

        def fake_get(path, params=None):
            if path == "/courses":
                return _response(courses)
            if path == "/courses/101/assignments":
                return _response(assignments)
            raise AssertionError(path)

        with mock.patch.object(canvas, "_get", side_effect=fake_get):
            all_open = canvas.get_incomplete_canvas_assignments()
            week = canvas.get_incomplete_canvas_assignments(days_ahead=7)
            no_overdue = canvas.get_incomplete_canvas_assignments(include_overdue=False, days_ahead=7)

        self.assertTrue(all_open["ok"])
        names = [item["assignment_name"] for item in all_open["assignments"]]
        self.assertEqual(names, ["Late quiz", "Soon essay", "Far future"])
        self.assertEqual(all_open["assignments"][0]["due_class"], "overdue")
        self.assertTrue(all_open["assignments"][0]["missing"])
        self.assertTrue(all_open["assignments"][0]["late"])
        self.assertEqual(all_open["assignments"][0]["course_code"], "COMP2022")
        self.assertNotIn("missing", all_open["assignments"][1])
        week_names = [item["assignment_name"] for item in week["assignments"]]
        self.assertEqual(week_names, ["Late quiz", "Soon essay"])
        self.assertEqual([item["assignment_name"] for item in no_overdue["assignments"]], ["Soon essay"])

    def test_assignment_status_uses_submission_not_due_date(self) -> None:
        past_due = (self.now - timedelta(days=1)).isoformat()
        course = {"id": 101, "name": "COMP2022", "course_code": "COMP2022"}
        assignment = {
            "id": 9,
            "name": "Assignment 1",
            "published": True,
            "due_at": past_due,
            "points_possible": 15,
            "submission_types": ["online_upload"],
            "html_url": "https://canvas.example.test/courses/101/assignments/9",
            "submission": {
                "workflow_state": "submitted",
                "submitted_at": "2026-09-23T03:00:00Z",
                "late": False,
                "missing": False,
                "score": None,
                "grade": None,
            },
        }

        def fake_get(path, params=None):
            if path == "/courses/101":
                return _response(course)
            if path == "/courses/101/assignments/9":
                self.assertEqual(params, [("include[]", "submission")])
                return _response(assignment)
            raise AssertionError(path)

        with mock.patch.object(canvas, "_get", side_effect=fake_get):
            status = canvas.get_canvas_assignment_status("101", "9")

        self.assertTrue(status["ok"])
        self.assertTrue(status["completed"])
        self.assertFalse(status["incomplete"])
        self.assertEqual(status["completion_state"], "submitted_not_graded")
        self.assertEqual(status["submission_status"], "submitted")
        self.assertEqual(status["submitted_at"], "2026-09-23T03:00:00Z")
        self.assertFalse(status["missing"])
        self.assertEqual(status["due_class"], "overdue")

    def test_get_never_puts_token_in_query(self) -> None:
        captured = {}

        def fake_get(url, params=None, headers=None):
            captured["url"] = url
            captured["params"] = params
            captured["headers"] = headers
            return _response([])

        with mock.patch("httpx.Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.get.side_effect = fake_get
            canvas._get("/courses", [("enrollment_state", "active")])

        self.assertNotIn("access_token", json.dumps(captured).lower())
        self.assertTrue(captured["headers"]["Authorization"].startswith("Bearer "))
        self.assertEqual(captured["headers"]["Authorization"], "Bearer test-token")


if __name__ == "__main__":
    unittest.main()
