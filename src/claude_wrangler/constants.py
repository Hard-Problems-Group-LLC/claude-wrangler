"""Project-wide constants for claude-wrangler."""

from pathlib import Path

from ._version import __version__

SCRIPT_NAME = "claude-wrangler"
SCRIPT_VERSION = __version__
SCHEMA_VERSION = 1
SCRIPT_MARKER = "managed by claude-wrangler"

CLAUDE_PACKAGE_NAME = "@anthropic-ai/claude-code"
# Claude Code publishes one npm dist-tag per channel, so unlike Codex the
# channel names below map one-to-one onto registry dist-tags.
CLAUDE_CHANNELS = ("stable", "latest", "next")
DEFAULT_INSTALL_CLAUDE_CHANNEL = "stable"
# `latest` remains the generic "current version of the selected channel"
# selector word; the channel decides which dist-tag actually resolves it.
DEFAULT_INSTALL_CLAUDE_SELECTOR = "latest"

# All session-specific Claude state lives under the `.local/claude/`
# umbrella: the npm install, the ring-1/2 redirected HOME, and the pinned
# CLAUDE_CONFIG_DIR contents. See
# docs/specifications/session_state_isolation_and_config_dir_pinning.md.
DEFAULT_LOCAL_DIR = ".local/claude/npm"
DEFAULT_HOME_DIR = ".local/claude/HOME"
DEFAULT_CONFIG_DIR = ".local/claude"
DEFAULT_LAUNCHER_RELATIVE_PATH = Path("bin") / "claude-local"
DEFAULT_README_FILENAME = "README-LOCAL-Start-Claude.md"
METADATA_FILENAME = ".claude-wrangler.json"
DEFAULT_NPM_TIMEOUT_SECONDS = 300
DEFAULT_NPM_INSTALL_LOGLEVEL = "http"
NPM_INSTALL_LOGLEVELS = (
    "silent",
    "error",
    "warn",
    "notice",
    "http",
    "timing",
    "info",
    "verbose",
    "silly",
)

GITIGNORE_BEGIN = "# BEGIN managed by claude-wrangler"
GITIGNORE_END = "# END managed by claude-wrangler"
README_MARKER = "<!-- managed by claude-wrangler -->"
