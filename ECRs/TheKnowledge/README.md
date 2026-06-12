# TheKnowledge ECRs

Use this directory for reviewable upstream requests aimed at TheKnowledge
when the active `TheKnowledge/` checkout in the consuming project is
effectively read-only.

Use the status subdirectories below this README:
- `open/` for drafted requests not yet under active upstream handling
- `in-progress/` for requests currently being carried into a writable
  TheKnowledge workflow
- `closed/` for requests whose upstream disposition is recorded

Keep each request in a clearly named file so another operator or AI agent can
carry it into a writable TheKnowledge checkout later.

When the change is ready to be filed or implemented upstream, use the
TheKnowledge `Feedback` branch workflow or direct writable maintenance in the
real TheKnowledge checkout. When that upstream handling begins, move the
source ECR into `in-progress/`. When the writable TheKnowledge proposal,
implementation, rejection, or deferral record settles the request, move the
source ECR into `closed/` and add a note pointing back to the settling
record. When you need to confirm acceptance later, compare the filename
against the writable TheKnowledge checkout's
`internal/overrides/proposals/accepted-ecrs-list.md`. Do not treat this
directory as TheKnowledge's own live state.
