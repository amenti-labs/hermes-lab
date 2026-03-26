# Hermes Lab - README FIRST

Read these files in order before touching experiment state:

1. `LAB-STATUS.md`
2. `LAB-INDEX.json`
3. `PROGRAM.md`
4. `experiments/<id>/RUNBOOK.md`
5. `experiments/<id>/SUMMARY.md`
6. `experiments/<id>/NEXT.md`
7. `experiments/<id>/SPEC.yaml` only if you need more detail

Rules:

- Acquire a lease before writing anything.
- Only write inside the claimed run bundle.
- If you are executing from `dispatch/running/<dispatch-id>/`, only write inside `run/`.
- Treat run bundles as immutable after sealing.
- Keep execution bounded by `time_budget_minutes`.
- Regenerate `LAB-STATUS.md` after reduction.
- Regenerate `LAB-INDEX.json` with the status view after reduction.
- Use `checkpoints/latest-run.txt` and `checkpoints/best-run.txt` for quick orientation.
