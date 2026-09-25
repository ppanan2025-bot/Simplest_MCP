---
name: onenote-mcp
description: >
  Read the user's personal OneNote via simplest-mcp. Use for notebooks,
  sections, pages, and handwriting. Read attached ink tiles. Never call
  vision_analyze, tesseract, or cron for OneNote.
version: 0.2.0
author: AnPan (ppanan2025-bot)
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [onenote, mcp, notes, microsoft-graph, read-only]
    related_skills: [knowledge-hub-mcp]
---

# OneNote via simplest-mcp

The user's OneNote is reachable through `simplest-mcp`. These tools are
**read-only**.

## When to Use

- "in my OneNote", "my notes", or what a notebook/page says
- A unit code (ISYS2120, COMP2022, ...) meaning their personal notebook,
  not PDFs in the knowledge hub

When ambiguous, check both OneNote and the hub.

## Tools

Names look like `mcp_simplest-mcp_<tool>` or `mcp__simplest_mcp__<tool>`.

1. `onenote_status()` — `authenticated` or not
2. `onenote_login()` — device-code URL if they need to sign in
3. `list_onenote_notebooks()`
4. `list_onenote_sections(notebook_id)`
5. `list_onenote_pages(section_id)` — titles only
6. `read_onenote_page(page_id)` — text plus attached images

## Procedure

1. Check status; give the device-code URL if not signed in.
2. Find the notebook. Unit notes may live in a unit notebook (`Comp2022`)
   **or** as a section inside `draft` (e.g. `draft → Comp2022 → recap`).
   Check both.
3. List sections, then pages. Quote Notebook → Section → Page.
4. Open the page with `read_onenote_page`. Do not answer from the title.

## Handwritten ink

`has_ink: true` means the body text is only a pointer. The handwriting
is in the **images attached to that same tool result**, top to bottom
(`ink_tiles`, files `01.png` …).

**Read those attached images yourself and transcribe them.**

Do **not**:

- call `vision_analyze`
- install or run tesseract
- schedule cron / retry jobs
- reuse old PNGs from `/workspace/onenote-pages/`
- tell the user the vision backend is down

If a stroke is genuinely unreadable, mark that bit `[illegible]`. Still
transcribe everything you can see. The recap page is messy student ink,
not a blank or a black blob.

`read_onenote_page` re-renders from Graph every time. Use the new tiles.

## Search is blind to ink and clipped slides

`unified_search` only matches typed/indexed text. `search_onenote` searches
titles and often returns HTTP 400 on this consumer account — do not retry
it. Traverse notebooks → sections → pages instead.

Clipped lecture slides may have little text and several images. Read the
attached images from `read_onenote_page` the same way. Do not call
`vision_analyze`.

## Pitfalls

- Empty title pages can still have images. Always open the page.
- Several lookalike places exist (`Comp2022` notebook vs `draft → Comp2022`).
  The recap page is `draft → Comp2022 → recap`.
- Distinguish "not found" from "page exists but empty".
- Knowledge hub tools are a separate store on the same server.
