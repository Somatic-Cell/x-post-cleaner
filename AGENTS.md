# Project review rules

This is an initial implementation to review collaboratively, not a certified production tool.

- Never weaken user/account/text matching to make DELETE succeed.
- Never label a timeout, missing post, 404, or interrupted DELETE as confirmed success.
- Never retry a DELETE automatically. Keep UNKNOWN/FAILED states until explicit reconciliation.
- Never run integration tests against a real account, upload private content, or enable deletion
  without the user's explicit instruction for that action. Tests must use synthetic records.
- Never send demo or local-guard probabilities off as Jev predictions. Demo cannot issue DELETE.
- Never use Jev confidence as probability of DELETE_CANDIDATE.
- Never silently truncate content, skip unknown archive structures, or fall back from translation
  to original-language classification under the same profile ID.
- Never infer medical conditions. Classify content against a user-defined publication policy.
- Keep SQLite, exports, recordings, archives and secrets outside version control.
- Add regression tests for every changed safety condition. Preserve API contracts and record
  sources in docs/API_REFERENCES.md; state what has and has not been tested.
- Do not run auto-fix changes across the codebase during a focused review without explaining scope.
