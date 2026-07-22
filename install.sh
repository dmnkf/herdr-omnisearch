#!/usr/bin/env sh
set -eu

usage() {
    cat <<'EOF'
Install herdr-omnisearch from this checkout.

Usage:
  ./install.sh [options]

Options:
  --mode wrapper|pip       Install mode. Default: wrapper.
  --bin-dir PATH           Where to install the herdr-omnisearch command.
                           Default: ~/.local/bin
  --config PATH            OmniSearch config path.
                           Default: ~/.config/herdr-omnisearch/config.ini
  --force-config           Replace an existing OmniSearch config.
  --herdr-config PATH      Herdr config to patch with command keybindings.
                           Default: ~/.config/herdr/config.toml
  --herdr-bin COMMAND      Herdr binary for OmniSearch config. Default: herdr
  --no-keybindings         Do not patch Herdr keybindings.
  --no-plugin              Do not link this checkout as a Herdr plugin.
  --no-watcher             Do not start the event-driven live index watcher.
  --dry-run                Print actions without writing files.
  -h, --help               Show this help.

The default wrapper mode is intentionally offline-friendly: it writes a small
~/.local/bin/herdr-omnisearch wrapper that points at this copied checkout.
EOF
}

die() {
    printf '%s\n' "install.sh: $*" >&2
    exit 1
}

say() {
    printf '%s\n' "$*"
}

need_value() {
    [ $# -ge 2 ] || die "missing value for $1"
}

shell_quote() {
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=${PYTHON:-python3}
MODE=wrapper
BIN_DIR=${BIN_DIR:-"$HOME/.local/bin"}
CONFIG_BASE=${XDG_CONFIG_HOME:-"$HOME/.config"}
CONFIG_FILE=${HERDR_OMNISEARCH_CONFIG:-"$CONFIG_BASE/herdr-omnisearch/config.ini"}
HERDR_CONFIG=${HERDR_CONFIG_PATH:-"$CONFIG_BASE/herdr/config.toml"}
HERDR_BIN=${HERDR_BIN:-herdr}
INSTALL_KEYBINDINGS=1
INSTALL_PLUGIN=1
START_WATCHER=1
FORCE_CONFIG=0
DRY_RUN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --mode)
            need_value "$@"
            MODE=$2
            shift 2
            ;;
        --bin-dir)
            need_value "$@"
            BIN_DIR=$2
            shift 2
            ;;
        --config)
            need_value "$@"
            CONFIG_FILE=$2
            shift 2
            ;;
        --force-config)
            FORCE_CONFIG=1
            shift
            ;;
        --herdr-config)
            need_value "$@"
            HERDR_CONFIG=$2
            shift 2
            ;;
        --herdr-bin)
            need_value "$@"
            HERDR_BIN=$2
            shift 2
            ;;
        --no-keybindings)
            INSTALL_KEYBINDINGS=0
            shift
            ;;
        --no-plugin)
            INSTALL_PLUGIN=0
            START_WATCHER=0
            shift
            ;;
        --no-watcher)
            START_WATCHER=0
            shift
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1"
            ;;
    esac
done

[ -d "$ROOT_DIR/src/herdr_omnisearch" ] || die "run this from an unpacked herdr-omnisearch checkout"
command -v "$PYTHON" >/dev/null 2>&1 || die "python interpreter not found: $PYTHON"

case "$MODE" in
    wrapper|pip) ;;
    *) die "unsupported --mode: $MODE" ;;
esac

install_wrapper() {
    target="$BIN_DIR/herdr-omnisearch"
    if [ "$DRY_RUN" -eq 1 ]; then
        say "would write wrapper: $target"
        return
    fi
    mkdir -p "$BIN_DIR"
    root_q=$(shell_quote "$ROOT_DIR/src")
    python_q=$(shell_quote "$PYTHON")
    tmp="$target.tmp.$$"
    cat > "$tmp" <<EOF
#!/usr/bin/env sh
export PYTHONPATH=$root_q:\${PYTHONPATH:-}
exec $python_q -m herdr_omnisearch "\$@"
EOF
    chmod 755 "$tmp"
    mv "$tmp" "$target"
    say "installed wrapper: $target"
}

install_pip() {
    if [ "$DRY_RUN" -eq 1 ]; then
        say "would run: $PYTHON -m pip install --user -e $ROOT_DIR"
        return
    fi
    if "$PYTHON" -m pip --version >/dev/null 2>&1; then
        "$PYTHON" -m pip install --user -e "$ROOT_DIR"
    else
        (cd "$ROOT_DIR" && "$PYTHON" setup.py develop --user)
    fi
}

install_config() {
    config_dir=$(dirname -- "$CONFIG_FILE")
    if [ -f "$CONFIG_FILE" ] && [ "$FORCE_CONFIG" -ne 1 ]; then
        say "kept existing config: $CONFIG_FILE"
        return
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        if [ -f "$CONFIG_FILE" ]; then
            say "would replace config: $CONFIG_FILE"
        else
            say "would create config: $CONFIG_FILE"
        fi
        return
    fi
    mkdir -p "$config_dir"
    cp "$ROOT_DIR/config.example.ini" "$CONFIG_FILE"
    "$PYTHON" - "$CONFIG_FILE" "$HERDR_BIN" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
herdr_bin = sys.argv[2]
text = path.read_text(encoding="utf-8")
if herdr_bin != "herdr":
    text = text.replace("bin = herdr", f"bin = {herdr_bin}", 1)
path.write_text(text, encoding="utf-8")
PY
    say "installed config: $CONFIG_FILE"
}

install_keybindings() {
    if [ "$INSTALL_KEYBINDINGS" -ne 1 ]; then
        return 0
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        say "would patch Herdr keybindings: $HERDR_CONFIG"
        return
    fi
    "$PYTHON" - "$HERDR_CONFIG" <<'PY'
from pathlib import Path
import re
import sys

config = Path(sys.argv[1]).expanduser()
config.parent.mkdir(parents=True, exist_ok=True)
text = config.read_text(encoding="utf-8") if config.exists() else ""

block = "\n".join(
    [
        "# BEGIN herdr-omnisearch",
        '[[keys.command]]',
        'key = "cmd+o"',
        'type = "plugin_action"',
        'command = "herdr.omnisearch.open-live"',
        'description = "OmniSearch"',
        "",
        '[[keys.command]]',
        'key = "cmd+shift+o"',
        'type = "plugin_action"',
        'command = "herdr.omnisearch.open-archive"',
        'description = "ArchiveSearch"',
        "# END herdr-omnisearch",
        "",
    ]
)

pattern = re.compile(r"\n?# BEGIN herdr-omnisearch\n.*?# END herdr-omnisearch\n?", re.S)
if pattern.search(text):
    text = pattern.sub("\n" + block, text).rstrip() + "\n"
else:
    if text and not text.endswith("\n"):
        text += "\n"
    if text:
        text += "\n"
    text += block

# Remove the pre-plugin command-pane bindings installed by older releases.
legacy = re.compile(
    r'\n?(?:# Generated from local Herdr config for remote runtime sessions\.\n)?'
    r'\[\[keys\.command\]\]\n'
    r'key = "cmd\+o"\n'
    r'type = "pane"\n'
    r'command = ".*?herdr-omnisearch pick .*?"\n'
    r'description = "OmniSearch"\n\n'
    r'\[\[keys\.command\]\]\n'
    r'key = "cmd\+shift\+o"\n'
    r'type = "pane"\n'
    r'command = ".*?herdr-omnisearch archive-pick .*?"\n'
    r'description = "ArchiveSearch"\n?',
    re.S,
)
text = legacy.sub("\n", text).lstrip("\n")
config.write_text(text, encoding="utf-8")
PY
    say "installed Herdr keybindings: $HERDR_CONFIG"
}

install_plugin() {
    if [ "$INSTALL_PLUGIN" -ne 1 ]; then
        return 0
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        say "would link Herdr plugin: $ROOT_DIR"
        [ "$START_WATCHER" -eq 1 ] && say "would start OmniSearch watcher"
        return 0
    fi
    "$HERDR_BIN" plugin link "$ROOT_DIR"
    plugin_config_dir=$("$HERDR_BIN" plugin config-dir herdr.omnisearch)
    if [ -f "$CONFIG_FILE" ] && [ ! -f "$plugin_config_dir/config.ini" ]; then
        cp "$CONFIG_FILE" "$plugin_config_dir/config.ini"
        say "seeded plugin config: $plugin_config_dir/config.ini"
    fi
    if [ "$START_WATCHER" -eq 1 ]; then
        "$HERDR_BIN" plugin action invoke watch-start --plugin herdr.omnisearch
    fi
}

reload_herdr_config() {
    if [ "$DRY_RUN" -eq 1 ]; then
        return 0
    fi
    "$HERDR_BIN" server reload-config
}

case "$MODE" in
    wrapper) install_wrapper ;;
    pip) install_pip ;;
esac
install_config
install_plugin
install_keybindings
reload_herdr_config

say ""
say "Done."
say "Make sure $BIN_DIR is on PATH, then run:"
say "  herdr-omnisearch doctor"
if [ "$INSTALL_KEYBINDINGS" -eq 1 ]; then
    say "cmd+o and cmd+shift+o now open managed OmniSearch plugin panes."
fi
