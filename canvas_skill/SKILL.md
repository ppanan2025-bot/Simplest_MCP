---
name: canvas-mcp
description: >
  Check the user's unfinished Canvas assignments and submission status
  through simplest-mcp tools only. Use when they ask what is due, what
  they have not submitted, or whether a specific assignment was handed in.
  Do not open a browser, and do not use the terminal or curl against Canvas.
version: 0.1.0
author: AnPan (ppanan2025-bot)
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [canvas, assignments, mcp, courses]
---

# Canvas MCP

Canvas status comes from the host MCP (`simplest-mcp`) using the signed-in
student token. Call the MCP tools. Do not start Chrome, do not ask the user
to log into Canvas in a browser, and do not read `.env`.

These tools are read-only. They cannot submit, grade, or change Canvas.

## When to Use

- "Which assignments haven't I finished?"
- "What unfinished work do I have this week?"
- "Did I submit my COMP2022 assignment?"
- Any question about Canvas due dates, missing work, or submission state.

## Tools

Names on Hermes look like `mcp_simplest-mcp_<tool>`.

1. `get_incomplete_canvas_assignments(include_overdue=True, include_future=True, days_ahead=None)`
   Unfinished work across current courses. Uses the user's submission, not
   due dates alone. For "this week", pass `days_ahead=7`.
2. `get_canvas_assignment_status(course_id, assignment_id)`
   One assignment: submitted, submitted but not graded, graded, excused, or
   missing.

## Procedure

1. For a list of unfinished work, call `get_incomplete_canvas_assignments`.
2. Summarize by course and `due_class` (`overdue`, `due_soon`, `upcoming`,
   `no_due_date`). Use `due_in` and `submission_status` from the tool.
3. A missing grade after `submitted` is finished work. Do not call that
   incomplete.
4. For one named assignment, find it from the list (or known ids), then call
   `get_canvas_assignment_status`.
5. If a tool returns `AUTH_NOT_CONFIGURED` or `AUTH_FAILED`, say Canvas is
   not connected on the MCP host. Do not fall back to a browser.

## Pitfalls

- Do not use a browser tool or ask the user to open Canvas.
- Do not invent submission state.
- Portal / optional LTI items may appear only when Canvas marks them missing.
- Start from a new Hermes session after Canvas env changes.
