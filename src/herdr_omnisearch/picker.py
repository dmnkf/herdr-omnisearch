import base64
import curses
import shutil
import subprocess
import sys

from .herdr_socket import HerdrError
from .settings import DEFAULT_LINES, app_config, cli_command_string, data_dir
from .storage import lock_is_held
from .textmatch import (
    clean_text,
    fuzzy_score_threshold,
    parse_filters,
    score_token_candidate,
    shorten,
    tokens,
)
from .live_index import (
    fuzzy_snippet,
    grouped_search_index,
    index_session,
    is_machine_row,
    is_workspace_row,
    machine_name,
    maybe_background_index,
)
from .machines import machine_statuses, maybe_background_sync, machines_config
from .archive_catalog import (
    archive_catalog_index,
    archive_catalog_max_window_offset,
    archive_catalog_search,
    archive_catalog_state,
    archive_window_label,
    maybe_background_archive_catalog_index,
    require_archive_enabled,
)
from .render import display_status, format_result, machine_prefix, tree_indent
from .navigate import focus_archive_catalog_result, focus_archive_row, focus_result, focus_row, row_client

ARCHIVE_PICKER_DEBOUNCE_MS = 100


ARCHIVE_PICKER_MIN_QUERY_CHARS = 3


def pick(args) -> int:
    if args.refresh:
        count = index_session(args.lines, args.include_empty, args.include_wrappers)
        if args.verbose:
            print(f"indexed {count} chunks", file=sys.stderr)
    elif args.background_refresh:
        maybe_background_index(args.lines, args.include_empty, args.include_wrappers, args.stale_seconds)
    if merges_machines(args) and (args.refresh or args.background_refresh):
        maybe_background_sync(min(args.stale_seconds, machines_config()["sync_seconds"]))
    if args.native or (not args.fzf and sys.stdin.isatty() and sys.stdout.isatty()):
        return curses.wrapper(lambda stdscr: curses_picker(stdscr, args))
    return fzf_picker(args)


def merges_machines(args) -> bool:
    return not getattr(args, "local_only", False) and machines_config()["enabled"]


def archive_pick(args) -> int:
    args.archive = True
    args.window_days = args.window_days or app_config()["archive_window_days"]
    args.window_offset = max(0, args.window_offset)
    require_archive_enabled()
    if args.refresh:
        changed, unchanged, removed, message_count = archive_catalog_index(args.agents)
        if args.verbose:
            print(
                f"cataloged {changed} changed / {unchanged} unchanged / "
                f"{removed} removed sessions ({message_count} messages)",
                file=sys.stderr,
            )
    elif args.background_refresh:
        maybe_background_archive_catalog_index(args.agents, args.stale_seconds)
    count, _last = archive_catalog_state()
    if not count:
        args.initial_message = (
            "Building the archive catalog in the background..."
            if lock_is_held(data_dir() / "archive-catalog.lock")
            else "Archive catalog is empty; refresh the archive index"
        )
    if args.native or (not args.fzf and sys.stdin.isatty() and sys.stdout.isatty()):
        return curses.wrapper(lambda stdscr: curses_picker(stdscr, args))
    return fzf_picker(args)


def fzf_picker(args) -> int:
    query = " ".join(args.query)
    rows = [row for row in picker_rows(args, query) if not is_machine_row(row)]
    if not rows:
        print("No OmniSearch matches.", file=sys.stderr)
        return 1
    fzf = shutil.which("fzf")
    if not fzf:
        for row in rows:
            print(format_result(row, multiline=True))
        return 1
    input_text = "\n".join(format_result(row) for row in rows)
    proc = subprocess.run(
        [
            fzf,
            "--delimiter",
            "\t",
            "--with-nth",
            "2,3,4",
            "--nth",
            "2,3,4",
            "--preview",
            f"{cli_command_string()} preview {{1}}",
            "--preview-window",
            "down,45%,wrap",
            "--header",
            picker_help(args),
            "--border",
            "rounded",
            "--border-label",
            f" {picker_title(args, query)} ",
            "--preview-label",
            " context ",
            "--highlight-line",
            "--track",
            "--prompt",
            f"{picker_title(args, query)}> ",
            "--height",
            "100%",
            "--layout",
            "reverse",
        ],
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return proc.returncode
    stable_id = proc.stdout.split("\t", 1)[0].strip()
    return picker_focus(args, stable_id)


def addnstr_safe(stdscr, y, x, text, width, attr=0):
    if y < 0 or x < 0 or width <= 0:
        return
    try:
        stdscr.addnstr(y, x, text, width, attr)
    except curses.error:
        pass


def status_attr(status):
    if status == "archive":
        return curses.color_pair(1)
    if status == "machine":
        return curses.color_pair(8) | curses.A_BOLD
    if status == "workspace":
        return curses.color_pair(1) | curses.A_BOLD
    if status == "working":
        return curses.color_pair(2) | curses.A_BOLD
    if status == "blocked":
        return curses.color_pair(3) | curses.A_BOLD
    if status == "idle":
        return curses.color_pair(4)
    return curses.color_pair(5)


def init_curses_colors():
    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_GREEN, -1)
        curses.init_pair(3, curses.COLOR_RED, -1)
        curses.init_pair(4, curses.COLOR_BLUE, -1)
        curses.init_pair(5, curses.COLOR_YELLOW, -1)
        curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_WHITE)
        curses.init_pair(7, curses.COLOR_BLACK, curses.COLOR_YELLOW)
        curses.init_pair(8, curses.COLOR_MAGENTA, -1)
    except curses.error:
        pass


def row_title(row):
    if row.get("source") == "archive":
        agent = row.get("agent") or "agent"
        space = row.get("workspace_label") or "archive"
        date = (row.get("updated_at") or row.get("started_at") or "archive")[:10]
        if row.get("match_content"):
            summary = fuzzy_snippet(
                row["match_content"],
                row.get("matched_tokens") or [],
            )
            role = row.get("match_role") or "message"
            summary = f"{role}: {summary}"
        else:
            preview_lines = clean_text(row.get("content") or "").splitlines()
            summary = preview_lines[-1] if preview_lines else row.get("title") or "session"
        summary = shorten(summary, 76)
        matches = row.get("match_count")
        count = f" x{matches}" if matches and int(matches) > 1 else ""
        return f"[archive] {space} / {agent} / {summary} / {date}{count}"
    if is_machine_row(row):
        spaces = row.get("match_count") or 0
        detail = row.get("_machine_detail") or ""
        return f"[machine] {machine_name(row)} · {spaces} space{'s' if spaces != 1 else ''}{detail}"
    if is_workspace_row(row):
        workspace = row.get("workspace_label") or row.get("workspace_id")
        matches = row.get("match_count")
        count = f" x{matches}" if matches and int(matches) > 1 else ""
        return f"{tree_indent(row)}[workspace] {workspace}{count}"
    label = row.get("pane_label") or row.get("pane_id")
    agent = row.get("agent") or "shell"
    status = display_status(row)
    workspace = row.get("workspace_label") or row.get("workspace_id")
    matches = row.get("match_count")
    count = f" x{matches}" if matches and int(matches) > 1 else ""
    marker = "★ " if row.get("_top_match") else ""
    return f"{tree_indent(row)}{marker}[{status}] {machine_prefix(row)}{workspace} / {agent} / {label}{count}"


def query_terms_for_display(query: str):
    stripped_query, _filters = parse_filters(query)
    return tokens(stripped_query)


def highlight_terms(row, query: str):
    query_terms = query_terms_for_display(query)
    if not query_terms:
        return []
    terms = set(query_terms)
    for token in row.get("matched_tokens") or []:
        if token:
            terms.add(token.lower())
    return sorted(terms, key=len, reverse=True)


def preview_lines_for_match(content: str, terms, limit: int):
    lines = clean_text(content or "").splitlines()
    limit = max(0, int(limit))
    if not lines or limit == 0:
        return []
    needles = [term.lower() for term in terms if term]
    matches = [
        index
        for index, line in enumerate(lines)
        if any(needle in line.lower() for needle in needles)
    ]
    if not matches:
        return lines[:limit]
    match = matches[-1]
    start = max(0, match - (limit // 2))
    start = min(start, max(0, len(lines) - limit))
    return lines[start : start + limit]


def find_highlight_spans(text: str, terms):
    spans = []
    lowered = text.lower()
    for term in terms:
        term = (term or "").lower()
        if len(term) < 2:
            continue
        start = 0
        while True:
            idx = lowered.find(term, start)
            if idx < 0:
                break
            spans.append((idx, idx + len(term)))
            start = idx + max(1, len(term))
    if not spans:
        return []
    spans.sort()
    merged = []
    for start, end in spans:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def add_highlighted(stdscr, y, x, text, width, base_attr=0, highlight_attr=0, terms=None):
    if y < 0 or x < 0 or width <= 0:
        return
    text = text[:width]
    spans = find_highlight_spans(text, terms or [])
    if not spans:
        addnstr_safe(stdscr, y, x, text, width, base_attr)
        return
    cursor = 0
    col = x
    for start, end in spans:
        if start > cursor:
            segment = text[cursor:start]
            addnstr_safe(stdscr, y, col, segment, width - (col - x), base_attr)
            col += len(segment)
        segment = text[start:end]
        addnstr_safe(stdscr, y, col, segment, width - (col - x), highlight_attr or (base_attr | curses.A_BOLD))
        col += len(segment)
        cursor = end
    if cursor < len(text):
        addnstr_safe(stdscr, y, col, text[cursor:], width - (col - x), base_attr)


def mode_title(mode: str) -> str:
    if mode == "normal":
        return "NORMAL"
    if mode == "action":
        return "ACTION"
    return "INSERT"


def render_picker(
    stdscr,
    query,
    rows,
    selected,
    *,
    title="Herdr OmniSearch",
    help_text=None,
    mode="insert",
    action_query="",
    actions=None,
    action_selected=0,
    message="",
):
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    if height < 10 or width < 60:
        addnstr_safe(stdscr, 0, 0, f"{title} needs a larger terminal.", width - 1, curses.A_BOLD)
        stdscr.refresh()
        return

    list_height = max(5, min(height - 7, height // 2))
    preview_y = list_height + 4
    preview_height = max(1, height - preview_y - 1)

    addnstr_safe(stdscr, 0, 0, title, width - 1, curses.color_pair(1) | curses.A_BOLD)
    help_text = help_text or "Type to search chats | filters: status:working agent:codex workspace:api cwd:backend | Enter focus | Esc quit"
    addnstr_safe(stdscr, 1, 0, help_text, width - 1, curses.A_DIM)
    if mode == "action":
        prompt = f"-- {mode_title(mode)} -- :{action_query}"
    else:
        prompt = f"-- {mode_title(mode)} -- / {query}"
    addnstr_safe(stdscr, 2, 0, prompt, width - 1, curses.A_BOLD)
    if message:
        addnstr_safe(stdscr, 3, 0, shorten(message, width - 1), width - 1, curses.A_DIM)

    if not rows:
        if not message:
            addnstr_safe(stdscr, 4, 0, "No matches.", width - 1, curses.A_DIM)
        stdscr.refresh()
        return

    selected = max(0, min(selected, len(rows) - 1))
    visible = rows[:list_height]
    if selected >= list_height:
        start = selected - list_height + 1
        visible = rows[start : start + list_height]
    else:
        start = 0

    for offset, row in enumerate(visible):
        idx = start + offset
        y = 4 + offset
        selected_row = idx == selected
        attr = curses.color_pair(6) if selected_row else status_attr(display_status(row))
        terms = highlight_terms(row, query)
        highlight_attr = curses.color_pair(7) | curses.A_BOLD
        add_highlighted(stdscr, y, 0, row_title(row), width - 1, attr, highlight_attr, terms)
        cwd = row.get("cwd") or ""
        if width > 100 and row.get("source") != "archive":
            cwd_x = min(62, width // 2)
            add_highlighted(
                stdscr,
                y,
                cwd_x,
                shorten(cwd, width - cwd_x - 1),
                width - cwd_x - 1,
                attr,
                highlight_attr,
                terms,
            )

    addnstr_safe(stdscr, preview_y - 2, 0, "─" * max(0, width - 1), width - 1, curses.A_DIM)
    row = rows[selected]
    terms = highlight_terms(row, query)
    highlight_attr = curses.color_pair(7) | curses.A_BOLD
    preview_header = f"{row_title(row)} | {row.get('cwd') or ''}"
    add_highlighted(stdscr, preview_y - 1, 0, preview_header, width - 1, curses.A_BOLD, highlight_attr, terms)
    if mode == "action":
        actions = actions or []
        if not actions:
            addnstr_safe(stdscr, preview_y, 0, "No actions.", width - 1, curses.A_DIM)
            stdscr.refresh()
            return
        action_selected = max(0, min(action_selected, len(actions) - 1))
        for offset, action in enumerate(actions[:preview_height]):
            attr = curses.color_pair(6) if offset == action_selected else 0
            prefix = "> " if offset == action_selected else "  "
            line = f"{prefix}{action['name']:<18} {action['label']}"
            add_highlighted(stdscr, preview_y + offset, 0, line, width - 1, attr, highlight_attr, tokens(action_query))
        stdscr.refresh()
        return
    preview_lines = preview_lines_for_match(row.get("content") or "", terms, preview_height)
    for offset, line in enumerate(preview_lines):
        add_highlighted(stdscr, preview_y + offset, 0, line, width - 1, 0, highlight_attr, terms)
    stdscr.refresh()


def picker_rows(args, query, *, snippets=False):
    if getattr(args, "archive", False):
        if query.strip() and not archive_picker_query_ready(query):
            return []
        searching = bool(query.strip())
        return archive_catalog_search(
            query,
            args.limit,
            agent=args.agent,
            window_days=None if searching else args.window_days,
            window_offset=None if searching else args.window_offset,
        )
    rows = grouped_search_index(
        query,
        args.limit,
        status=getattr(args, "status", None),
        agent=args.agent,
        snippets=snippets,
        all_sessions=getattr(args, "all_sessions", False),
        machines=merges_machines(args),
    )
    if any(is_machine_row(row) for row in rows):
        annotate_machine_rows(rows)
    return rows


def annotate_machine_rows(rows):
    statuses = machine_statuses()
    for row in rows:
        if not is_machine_row(row) or not row.get("machine_id"):
            continue
        status = statuses.get(row["machine_id"]) or {}
        if status and not status.get("ok"):
            row["_machine_detail"] = " · offline, showing last sync"
        elif status.get("error"):
            row["_machine_detail"] = " · stale, remote index failed"
        row["content"] = status.get("error") or ""


def picker_focus(args, result):
    row = result if isinstance(result, dict) else None
    stable_id = row.get("stable_id") if row is not None else result
    if getattr(args, "archive", False):
        if row is not None and row.get("_archive_catalog"):
            return focus_archive_row(row)
        return focus_archive_catalog_result(stable_id)
    if row is not None and row.get("machine_id"):
        return focus_row(row)
    return focus_result(stable_id)


def archive_picker_query_ready(query: str) -> bool:
    text, filters = parse_filters(query)
    has_metadata_filter = any(
        filters.get(name)
        for name in ("agent", "workspace", "cwd")
    )
    return has_metadata_filter or any(
        len(term) >= ARCHIVE_PICKER_MIN_QUERY_CHARS
        for term in tokens(text)
    )


def archive_picker_lookup_query(args, query: str) -> str:
    if (
        getattr(args, "archive", False)
        and query.strip()
        and not archive_picker_query_ready(query)
    ):
        return ""
    return query


def picker_title(args, query=""):
    if getattr(args, "archive", False):
        if query.strip() and archive_picker_query_ready(query):
            return "Herdr ArchiveSearch | all dates"
        days = getattr(args, "window_days", None) or app_config()["archive_window_days"]
        offset = getattr(args, "window_offset", 0)
        return f"Herdr ArchiveSearch | {archive_window_label(days, offset)}"
    return "Herdr OmniSearch"


def picker_help(args):
    if getattr(args, "archive", False):
        return "insert: type search | Esc normal | left older | right newer | Enter resume | q quit"
    return "insert: type search | Esc normal | normal: j/k gg G Enter focus a/: actions q quit | machine:name"


def clipboard_copy(text: str):
    text = text or ""
    if not text:
        return False, "nothing to yank"
    commands = [
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
        ["pbcopy"],
    ]
    for command in commands:
        if not shutil.which(command[0]):
            continue
        try:
            proc = subprocess.run(
                command,
                input=text,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0:
            return True, f"yanked {len(text)} chars"

    # OSC52 works well through many terminal setups, including Kitty, and avoids
    # adding a hard clipboard dependency.
    try:
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        with open("/dev/tty", "w", encoding="utf-8", errors="ignore") as tty:
            tty.write(f"\x1b]52;c;{encoded}\a")
            tty.flush()
        return True, f"yanked {len(text)} chars via OSC52"
    except OSError:
        return False, "no clipboard path available"


def action_specs(args, row):
    archive = row.get("source") == "archive" or getattr(args, "archive", False)
    specs = []
    if is_machine_row(row):
        return []
    focus_label = "focus existing or resume archive session" if archive else "focus exact selected row"
    if row.get("machine_id"):
        focus_label = f"focus on {machine_name(row)}, then select that machine"
    specs.append({"name": "focus", "label": focus_label})

    # Renames target the local socket; synced rows belong to another server.
    if not archive and not row.get("machine_id"):
        if row.get("workspace_id"):
            specs.append({"name": "rename-workspace", "label": "rename workspace"})
        if row.get("pane_id") and not is_workspace_row(row):
            specs.append({"name": "rename-pane", "label": "rename pane"})

    yank_values = [
        ("yank-cwd", "yank cwd", row.get("cwd") or row.get("foreground_cwd") or ""),
        ("yank-session", "yank agent session id", row.get("session_id") or row.get("agent_session_id") or ""),
        ("yank-pane", "yank pane id", "" if archive or is_workspace_row(row) else row.get("pane_id") or ""),
        ("yank-workspace", "yank workspace id", row.get("workspace_id") or ""),
    ]
    for name, label, value in yank_values:
        if value:
            specs.append({"name": name, "label": label, "value": value})
    return specs


def filter_actions(actions, query: str):
    terms = tokens(query)
    if not terms:
        return actions
    filtered = []
    for action in actions:
        haystack = f"{action['name']} {action['label']}"
        haystack_tokens = set(tokens(haystack))
        if all(any(score_token_candidate(term, token) >= fuzzy_score_threshold(term) for token in haystack_tokens) for term in terms):
            filtered.append(action)
    return filtered


def prompt_line(stdscr, prompt: str, initial: str = ""):
    value = initial or ""
    while True:
        height, width = stdscr.getmaxyx()
        line = f"{prompt}{value}"
        addnstr_safe(stdscr, height - 1, 0, " " * max(0, width - 1), width - 1, 0)
        addnstr_safe(stdscr, height - 1, 0, line, width - 1, curses.A_BOLD)
        try:
            stdscr.move(height - 1, min(len(line), width - 2))
        except curses.error:
            pass
        stdscr.refresh()
        key = stdscr.get_wch()
        if key in ("\x03", "\x1b"):
            return None
        if key in ("\n", "\r"):
            return value.strip()
        if key in ("\x15",):
            value = ""
            continue
        if key in ("\x7f", "\b", "\x08", "\x1f", "\x04"):
            value = value[:-1]
            continue
        if isinstance(key, int):
            if key in (curses.KEY_BACKSPACE, curses.KEY_DC):
                value = value[:-1]
            continue
        if isinstance(key, str) and key.isprintable():
            value += key


def refresh_picker_index(args):
    if getattr(args, "archive", False):
        return
    index_session(
        getattr(args, "lines", DEFAULT_LINES),
        getattr(args, "include_empty", False),
        getattr(args, "include_wrappers", False),
    )


def execute_action(stdscr, args, row, action):
    name = action["name"]
    if name == "focus":
        return picker_focus(args, row), "", False
    if name.startswith("yank-"):
        ok, message = clipboard_copy(action.get("value") or "")
        return None, message if ok else f"yank failed: {message}", False
    if name == "rename-workspace":
        label = prompt_line(stdscr, "workspace name: ", row.get("workspace_label") or "")
        if not label:
            return None, "rename cancelled", False
        try:
            row_client(row).rename_workspace(row["workspace_id"], label)
        except HerdrError as exc:
            return None, f"workspace rename failed: {exc}", False
        refresh_picker_index(args)
        return None, f"renamed workspace to {label}", True
    if name == "rename-pane":
        label = prompt_line(stdscr, "pane name: ", row.get("pane_label") or "")
        if not label:
            return None, "rename cancelled", False
        try:
            row_client(row).rename_pane(row["pane_id"], label)
        except HerdrError as exc:
            return None, f"pane rename failed: {exc}", False
        refresh_picker_index(args)
        return None, f"renamed pane to {label}", True
    return None, f"unknown action: {name}", False


def pending_keys(stdscr, idle_ms=0):
    if idle_ms:
        stdscr.timeout(idle_ms)
    else:
        stdscr.nodelay(True)
    try:
        while True:
            try:
                key = stdscr.get_wch()
                yield key
                if key == "\x1b":
                    break
            except curses.error:
                break
    finally:
        stdscr.timeout(-1)


def apply_insert_key(query: str, key):
    if key == "\x1b":
        return query, False, True
    if key in ("\x15",):
        return "", True, False
    if key in ("\x7f", "\b", "\x08", "\x1f", "\x04"):
        return query[:-1], True, False
    if isinstance(key, int):
        if key in (curses.KEY_BACKSPACE, curses.KEY_DC):
            return query[:-1], True, False
        return query, False, False
    if isinstance(key, str) and key.isprintable():
        return query + key, True, False
    return query, False, False


def cached_picker_rows(args, query, cache):
    cache_key = (
        bool(getattr(args, "archive", False)),
        getattr(args, "status", None),
        getattr(args, "agent", None),
        getattr(args, "window_days", None),
        getattr(args, "window_offset", 0),
        args.limit,
        query,
    )
    rows = cache.get(cache_key)
    if rows is None:
        rows = picker_rows(args, query, snippets=False)
        cache[cache_key] = rows
        if len(cache) > 128:
            cache.pop(next(iter(cache)))
    return rows


def curses_picker(stdscr, args) -> int:
    init_curses_colors()
    curses.noecho()
    curses.cbreak()
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.keypad(True)
    query = " ".join(args.query)
    selected = 0
    row_cache = {}
    rows = cached_picker_rows(
        args,
        archive_picker_lookup_query(args, query),
        row_cache,
    )
    mode = "insert"
    action_query = ""
    action_selected = 0
    pending = ""
    message = getattr(args, "initial_message", "")

    while True:
        if selected >= len(rows):
            selected = max(0, len(rows) - 1)
        selected_row = rows[selected] if rows else {}
        actions = filter_actions(action_specs(args, selected_row), action_query) if mode == "action" and rows else []
        if action_selected >= len(actions):
            action_selected = max(0, len(actions) - 1)
        render_picker(
            stdscr,
            query,
            rows,
            selected,
            title=picker_title(args, query),
            help_text=picker_help(args),
            mode=mode,
            action_query=action_query,
            actions=actions,
            action_selected=action_selected,
            message=message,
        )
        message = ""
        key = stdscr.get_wch()

        if key == "\x03":
            return 130
        if key == "\x1b":
            if mode == "insert":
                mode = "normal"
                pending = ""
                continue
            if mode == "action":
                mode = "normal"
                action_query = ""
                action_selected = 0
                pending = ""
                continue
            return 0

        if mode == "action":
            if key in ("\n", "\r"):
                if actions:
                    exit_code, action_message, refresh = execute_action(stdscr, args, rows[selected], actions[action_selected])
                    if exit_code is not None:
                        return exit_code
                    if refresh:
                        row_cache.clear()
                        rows = cached_picker_rows(args, query, row_cache)
                        selected = min(selected, max(0, len(rows) - 1))
                    message = action_message
                    mode = "normal"
                    action_query = ""
                    action_selected = 0
                continue
            if key in ("\x15",):
                action_query = ""
                action_selected = 0
                continue
            if key in ("\x7f", "\b", "\x08", "\x1f", "\x04"):
                action_query = action_query[:-1]
                action_selected = 0
                continue
            if isinstance(key, int):
                if key in (curses.KEY_BACKSPACE, curses.KEY_DC):
                    action_query = action_query[:-1]
                    action_selected = 0
                elif key == curses.KEY_UP:
                    action_selected = max(0, action_selected - 1)
                elif key == curses.KEY_DOWN:
                    action_selected = min(max(0, len(actions) - 1), action_selected + 1)
                continue
            if isinstance(key, str):
                if key in ("j", "\x0e"):
                    action_selected = min(max(0, len(actions) - 1), action_selected + 1)
                    continue
                if key in ("k", "\x10"):
                    action_selected = max(0, action_selected - 1)
                    continue
                if key.isprintable():
                    action_query += key
                    action_selected = 0
                continue

        if key in ("\n", "\r"):
            if rows and is_machine_row(rows[selected]):
                message = "Machine headers group results; pick a row below"
            elif rows:
                return picker_focus(args, rows[selected])
            continue

        if isinstance(key, int):
            if getattr(args, "archive", False) and key in (curses.KEY_LEFT, curses.KEY_RIGHT):
                delta = 1 if key == curses.KEY_LEFT else -1
                max_offset = archive_catalog_max_window_offset(args.agent, args.window_days)
                target = min(max_offset, max(0, args.window_offset + delta))
                if target == args.window_offset:
                    message = (
                        "Already showing the newest archive window"
                        if delta < 0
                        else "Already showing the oldest archive window"
                    )
                    continue
                args.window_offset = target
                row_cache.clear()
                rows = cached_picker_rows(
                    args,
                    archive_picker_lookup_query(args, query),
                    row_cache,
                )
                selected = 0
                message = f"Showing {archive_window_label(args.window_days, target)}"
            elif key in (curses.KEY_BACKSPACE, curses.KEY_DC):
                if mode == "insert":
                    query = query[:-1]
                    selected = 0
                    rows = cached_picker_rows(
                        args,
                        archive_picker_lookup_query(args, query),
                        row_cache,
                    )
            elif key == curses.KEY_UP:
                selected = max(0, selected - 1)
            elif key == curses.KEY_DOWN:
                selected = min(max(0, len(rows) - 1), selected + 1)
            elif key == curses.KEY_PPAGE:
                selected = max(0, selected - 10)
            elif key == curses.KEY_NPAGE:
                selected = min(max(0, len(rows) - 1), selected + 10)
            elif key == curses.KEY_HOME:
                selected = 0
            elif key == curses.KEY_END:
                selected = max(0, len(rows) - 1)
            elif key == curses.KEY_RESIZE:
                pass
            continue

        if mode == "normal" and isinstance(key, str):
            if pending == "g":
                pending = ""
                if key == "g":
                    selected = 0
                    continue
            if key == "g":
                pending = "g"
                continue
            pending = ""
            if key in ("q",):
                return 0
            if key in ("i", "/"):
                mode = "insert"
                continue
            if key == "c":
                query = ""
                selected = 0
                rows = cached_picker_rows(args, query, row_cache)
                mode = "insert"
                continue
            if key in ("a", ":"):
                mode = "action"
                action_query = ""
                action_selected = 0
                continue
            if key in ("j", "\x0e"):
                selected = min(max(0, len(rows) - 1), selected + 1)
                continue
            if key in ("k", "\x10"):
                selected = max(0, selected - 1)
                continue
            if key == "\x04":
                selected = min(max(0, len(rows) - 1), selected + 10)
                continue
            if key == "\x15":
                selected = max(0, selected - 10)
                continue
            if key == "G":
                selected = max(0, len(rows) - 1)
                continue
            continue

        if mode == "insert":
            query, changed, exit_insert = apply_insert_key(query, key)
            if changed:
                render_picker(
                    stdscr,
                    query,
                    rows,
                    selected,
                    title=picker_title(args, query),
                    help_text=picker_help(args),
                    mode=mode,
                )
            idle_ms = (
                ARCHIVE_PICKER_DEBOUNCE_MS
                if getattr(args, "archive", False)
                else 0
            )
            interrupted = False
            queued_keys = pending_keys(stdscr, idle_ms)
            try:
                for queued_key in queued_keys:
                    if queued_key == "\x03":
                        interrupted = True
                        break
                    query, queued_changed, queued_exit_insert = apply_insert_key(
                        query,
                        queued_key,
                    )
                    changed = changed or queued_changed
                    if queued_changed:
                        render_picker(
                            stdscr,
                            query,
                            rows,
                            selected,
                            title=picker_title(args, query),
                            help_text=picker_help(args),
                            mode=mode,
                        )
                    if queued_exit_insert:
                        exit_insert = True
                        break
            finally:
                queued_keys.close()
            if interrupted:
                return 130
            if exit_insert:
                mode = "normal"
                pending = ""
            if changed:
                selected = 0
                rows = cached_picker_rows(
                    args,
                    archive_picker_lookup_query(args, query),
                    row_cache,
                )
