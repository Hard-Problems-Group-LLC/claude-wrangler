# Engineering Change Requests

This directory is the local home for engineering change requests aimed at
systems outside the consuming project itself.

When a project uses `TheKnowledge/` read-only as a submodule or sibling
checkout, keep upstream TheKnowledge requests under the
`ECRs/TheKnowledge/` subtree. Draft new requests in
`ECRs/TheKnowledge/open/`, move them to `ECRs/TheKnowledge/in-progress/`
during active upstream handling, and move them to
`ECRs/TheKnowledge/closed/` once the upstream disposition is recorded. When
you need to confirm that a carried request was accepted or implemented
upstream, compare its filename against the writable TheKnowledge checkout's
`internal/overrides/proposals/accepted-ecrs-list.md`.
