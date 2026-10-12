# Recorded Claude Code Release Fixtures

These files are byte-exact copies of public release metadata that Anthropic
publishes. Offline signature tests use them (`tests/test_signing.py`). They
were recorded on 2026-10-12 with plain HTTPS GETs and written in binary mode.
`.gitattributes` disables Git's text conversion so that the signatures keep
verifying on Windows checkouts that use `core.autocrlf`.

| Fixture path | Source path under `https://downloads.claude.ai/` |
| --- | --- |
| `<version>/manifest.json` | `claude-code-releases/<version>/manifest.json` |
| `<version>/manifest.json.sig` | `claude-code-releases/<version>/manifest.json.sig` |
| `<version>/manifest.json.raw-sig.json` | `claude-code-releases/<version>/manifest.json.raw-sig.json` |
| `keys/claude-code.rsa.pub` | `keys/claude-code.rsa.pub` |
| `keys/claude-code.asc` | `keys/claude-code.asc` |

Recorded versions:

- `2.1.207`: the version floor; the first release with both signatures.
- `2.1.287`: the `stable` channel when these fixtures were recorded.
- `2.1.296`: the `latest` channel when these fixtures were recorded.

Anthropic's setup guide (`https://code.claude.com/docs/en/setup`) documents
SHA-256 `395759c1f7449ef4cdef305a42e820f3c766d6090d142634ebdb049f113168b6`
for `claude-code.rsa.pub` and OpenPGP fingerprint
`31DD DE24 DDFA B679 F42D 7BD2 BAA9 29FF 1A7E CACE` for `claude-code.asc`.
Both keys hold the same RSA-4096 key material. `tests/test_signing.py` pins
each file's SHA-256 and checks that the trust anchor compiled into
`claude_wrangler.signing` is derived from these keys.

Never add release binaries here. Tests must stay offline.
