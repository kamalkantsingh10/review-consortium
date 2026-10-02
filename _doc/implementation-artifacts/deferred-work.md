# Deferred Work

- source_spec: `_doc/implementation-artifacts/spec-1-4-push-clips-blind.md`
  summary: Reconcile orphans on connect (key rows or clips/*.mp4 with no board.db row, stale clips/.push-*.mp4) after a hard kill mid-push.
  evidence: The edge-case review showed SIGKILL or power loss between the key fsync / os.replace and COMMIT leaves fsynced key rows and a clip file with no DB row. Normal failures roll back correctly.
- source_spec: `_doc/implementation-artifacts/spec-1-3-seeded-persona-generation.md`
  summary: `personas generate --force` must refuse (or require explicit confirmation) once board.db holds Runs or ratings that reference Persona IDs.
  evidence: Regenerating after Runs exist would silently re-label past responses. There are no Runs until Story 1.7, so revisit when 1.7 lands.
- source_spec: `_doc/implementation-artifacts/spec-1-5-push-a-test.md`
  summary: The init template's tests/example.yaml has practice: [] while session.practice_clips defaults to 2, so it can never be pushed. Fix it as part of the bundled example Study (Story 4.5).
  evidence: The verification-gap review on 1.5; push test refuses with bad_practice on a fresh Study.
- source_spec: `_doc/implementation-artifacts/spec-1-8-resume-a-run-and-verify-re-issue.md`
  summary: Recovery for a stored handle the provider can no longer collect (expired or deleted), e.g. give that Trial a new attempt after a categorised collect failure. Add a collect-path concurrency test with the first real adapter.
  evidence: Today every --resume re-collects the same dead handle and stops. This only matters once real adapters exist (Epic 2).
- source_spec: `_doc/implementation-artifacts/spec-1-8-resume-a-run-and-verify-re-issue.md`
  summary: Expose the re-issue check as a CLI command (e.g. `consortium verify <test>`) and report the verified line count. It belongs with the Reporting manifest (Epic 4).
  evidence: FR24 "verify re-issue" is currently only callable from Python, and runs as a side effect of --resume.
