"""Project-wide constants for claude-wrangler."""

from pathlib import Path

from ._version import __version__

SCRIPT_NAME = "claude-wrangler"
SCRIPT_VERSION = __version__
# Schema 2 is the native A/B runtime metadata stored at
# `<runtime root>/.claude-wrangler.json`; schema 1 is the npm-era layout
# metadata that `legacy_npm_layout_migration.md` migrates from and keeps as
# a compatibility projection.
SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
SCRIPT_MARKER = "managed by claude-wrangler"

# The npm package name is only read during legacy-layout discovery and
# adopt-in-place migration; claude-wrangler never installs from npm.
CLAUDE_PACKAGE_NAME = "@anthropic-ai/claude-code"

# Native release channels (distribution.SUPPORTED_CHANNELS). The CLI also
# accepts `next` as a deprecated alias of `latest`.
CLAUDE_CHANNELS = ("stable", "latest")
CLI_CHANNEL_CHOICES = ("stable", "latest", "next")
DEFAULT_INSTALL_CLAUDE_CHANNEL = "stable"
# `latest` is the generic "current version of the selected channel"
# selector word; the channel decides which pointer resolves it.
DEFAULT_INSTALL_CLAUDE_SELECTOR = "latest"

# All session-specific Claude state lives under the `.local/claude/`
# umbrella: the native runtime root, the ring-1/2 redirected HOME, and the
# pinned CLAUDE_CONFIG_DIR contents. See
# docs/specifications/session_state_isolation_and_config_dir_pinning.md and
# docs/specifications/ab_runtime_slots.md.
DEFAULT_LOCAL_DIR = ".local/claude/wrangler"
LEGACY_LOCAL_DIR = ".local/claude/npm"
DEFAULT_HOME_DIR = ".local/claude/HOME"
DEFAULT_CONFIG_DIR = ".local/claude"
DEFAULT_LAUNCHER_RELATIVE_PATH = Path("bin") / "claude-local"
CMD_SHIM_SUFFIX = ".cmd"
DEFAULT_README_FILENAME = "README-LOCAL-Start-Claude.md"
METADATA_FILENAME = ".claude-wrangler.json"

# Generated files inside the runtime root.
LAUNCH_CORE_FILENAME = "launch.py"
LAUNCH_CONFIG_FILENAME = "launch.json"
LAUNCH_CONFIG_SCHEMA = 1
PATH_SHIM_DIRNAME = "shim"
PATH_SHIM_NAME = "claude"
UPDATE_CHECK_FILENAME = "update-check.json"
FALLBACK_FILENAME = "fallback.json"
CONFIG_SETTINGS_FILENAME = "settings.json"

DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 60
SMOKE_TEST_TIMEOUT_SECONDS = 60.0
HELP_READ_TIMEOUT_SECONDS = 60.0

# Environment the launcher exports so Claude Code never updates or
# reinstalls itself. Its updater targets HOME-relative paths rather than
# the running binary, so at ring 0 it would rewrite the operator's global
# install. DISABLE_UPDATES (Claude Code 2.1.118+) also blocks manual
# `claude update` and `claude install`; DISABLE_AUTOUPDATER covers older
# builds; DISABLE_INSTALLATION_CHECKS silences install-location nags.
SELF_UPDATE_SUPPRESSION_ENV = (
    ("DISABLE_UPDATES", "1"),
    ("DISABLE_AUTOUPDATER", "1"),
    ("DISABLE_INSTALLATION_CHECKS", "1"),
)
# Settings key the launcher's `--settings` and the config-dir settings.json
# both carry (cross_platform_launcher.md requirement 4).
DEEP_LINK_SETTING_KEY = "disableDeepLinkRegistration"
DEEP_LINK_SETTING_VALUE = "disable"

# How the launcher spells CLAUDE_CONFIG_DIR. `native` joins the repository
# root and the relative config dir with the host separator. On Windows,
# `forward-slash` reproduces the spelling a Git Bash launcher exported
# (`C:/.../.local/claude`) so a migrated project keeps the byte-identical
# value Claude Code keys some state on (legacy_npm_layout_migration.md 7).
CONFIG_DIR_SPELLING_NATIVE = "native"
CONFIG_DIR_SPELLING_FORWARD_SLASH = "forward-slash"
CONFIG_DIR_SPELLINGS = (CONFIG_DIR_SPELLING_NATIVE, CONFIG_DIR_SPELLING_FORWARD_SLASH)

GITIGNORE_BEGIN = "# BEGIN managed by claude-wrangler"
GITIGNORE_END = "# END managed by claude-wrangler"
README_MARKER = "<!-- managed by claude-wrangler -->"
