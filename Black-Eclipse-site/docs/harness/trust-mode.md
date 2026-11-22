# Trust Mode (one approval, automatic undo)

Trust Mode is the non-technical happy path: the user approves a **workspace**
once, then says what they want. Eclipse builds, verifies and lands it without a
second review screen. Every landing can be undone with one call.

The chat change card now offers **Apply now** next to **Review**: one click runs
the same autoland call (build, apply, quick checks, auto-undo on failure) and
leaves an **Undo** action. No diff, test log or approval code is required.

## What “one approval” means

1. The operator grants trust for a project once (the front door owns this
   toggle; the backend route below is already operator-gated).
2. The user asks for a change.
3. Eclipse builds the change in its disposable worktree, then calls
   `POST /code/tasks/{runId}/autoland`.
4. That one call:
   - applies the prepared change with the normal path-safety, digest and backup
     rules;
   - runs the landing checks against the project on disk;
   - keeps the change when checks pass;
   - **rolls it back automatically** when a check fails or the checker crashes.
5. The response carries `rollbackId`. Undo is the existing
   `POST /code/promotions/{rollbackId}/rollback`.

The user never inspects a diff or a test log unless they choose to. A failed
check is reported in plain language with the change already reverted.

## API

```http
POST /code/tasks/{runId}/autoland            # quick checks (fast, default)
POST /code/tasks/{runId}/autoland?full=true  # include the long test suite
POST /code/promotions/{rollbackId}/rollback  # undo
```

Response (success):

```json
{ "success": true, "ok": true, "applied": true, "rolledBack": false,
  "rollbackId": "…", "checks": { "ok": true }, "preview": { … } }
```

Response (failed check, already undone):

```json
{ "success": true, "ok": false, "applied": false, "rolledBack": true,
  "rollbackId": "…", "checks": { "ok": false, "failed": ["tests"] } }
```

`autoland` is implemented in `python/harness/code/autoland.py`; the route is in
`python/harness/routers/code.py`. The reviewed flow (`promote/preview` +
approval) remains available and unchanged.

## What still cannot happen

- Trust Mode never auto-lands a change that fails its checks, and never skips
  path protection, digest staleness or backup creation.
- It does not bypass the self-hosting authority refusal.
- It never pushes or commits to git.
- A code change still runs in the disposable worktree first; Trust Mode only
  removes the *human confirmation step*, not the build/verify/backup step.

## Verification

`python/tests/test_code_autoland.py`:

- lands a passing change and proves the existing rollback restores the original;
- a failed check auto-rolls back;
- a crashing checker is a failure and auto-rolls back;
- no prepared change is an honest `PROMOTION_NO_CHANGE`;
- the operator-gated route returns the outcome and rollback id.
