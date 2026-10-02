# Deferred Work

- source_spec: `_doc/implementation-artifacts/spec-1-4-push-clips-blind.md`
  summary: Reconcile orphans on connect (key rows or clips/*.mp4 with no board.db row, stale clips/.push-*.mp4) after a hard kill mid-push.
  evidence: The edge-case review showed SIGKILL or power loss between the key fsync / os.replace and COMMIT leaves fsynced key rows and a clip file with no DB row. Normal failures roll back correctly.
- source_spec: `_doc/implementation-artifacts/spec-1-3-seeded-persona-generation.md`
  summary: `personas generate --force` must refuse (or require explicit confirmation) once board.db holds Runs or ratings that reference Persona IDs.
  evidence: Regenerating after Runs exist would silently re-label past responses. There are no Runs until Story 1.7, so revisit when 1.7 lands.
