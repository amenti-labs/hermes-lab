# Hermes Lab - Agent Entry Point

`README-FIRST.md` is the canonical ingress card. This file exists as a compatibility alias.

Read these files in order:

1. `LAB-STATUS.md`
2. `LAB-INDEX.json`
3. `PROGRAM.md`
4. `experiments/<id>/RUNBOOK.md`
5. `experiments/<id>/SUMMARY.md`
6. `experiments/<id>/NEXT.md`

Operating rules:

- Acquire a lease before writing.
- Only write inside the claimed run bundle.
- If you are working from `dispatch/running/<dispatch-id>/`, only write inside `run/`.
- Do not hand-edit derived files.
- Write `RESULT.md` before reduction updates any projections.
