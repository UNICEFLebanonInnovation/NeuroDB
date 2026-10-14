"""Parity and timing of pages that load in parts (SPEC7 §3.5, §3.6): the acceptance step of each stage.

A stage changes how a page is drawn (a frame first, then each data block in a request of its own), never its
figures. This tool proves it on one database snapshot of a large world, outside the test suite, because the
old code only exists in a git worktree of the stage's base commit:

1. **World.** ``scale.py`` multiplies the demo world (``seed_demo``) on a database named
   ``neurodb_parity_<stage>``; it refuses every other name. Take one snapshot (``pg_dump -Fc``).
2. **Record** on the base commit (a separate worktree), on a fresh restore of the snapshot::

       python -m scripts.parity.core record overview base.json --checkout /path/to/base-worktree

   For an administrator and a viewer with a section, over the group's matrix of filters, it keeps the
   figures at the page's context paths, the visible text and the links of each block (keyed by heading),
   the element ids and every ``json_script`` payload, with the clock frozen.
3. **Check** on the working tree, on another fresh restore of the same snapshot, migrated to the head::

       python -m scripts.parity.core check overview base.json

   The same requests go through the shell and its part requests (HTMX, as the browser asks for them), the
   part contexts merged; the group module maps the context paths that moved. Any difference fails.
4. **Timing** (``timing.py``): the shell cold and warm, its queries, the time to the first figure and to the
   last part (the browser's queue simulated), and a load check under gunicorn.

Run every command from the repository root of the working tree (``python -m scripts.parity...``):
``--checkout`` points the run at the other worktree's code. Nothing here imports the new code at module
level, so the same files record on a base commit that does not have it.

Modules: ``core`` (the command line, the frozen clock, assembling a page, reading it, comparing),
``overview`` (stage 0: the country overview's matrix and context paths), ``reports_core`` (stage A: the
management brief, the partner profile and data health), the four groups of step 3: ``etools`` (partner
monitoring and its map, Progress reports and funds), ``fmm`` (Monitoring insights, every tab),
``other_d1`` (the country programme, Makani and Dirasa, youth and the donor page) and ``other_d2`` (the
knowledge search and the document review), each with the world it needs described in its docstring,
``scale`` (the world), ``timing`` (speed), ``settings`` (the timing runs' settings: production's caches,
no collected static files).
"""
