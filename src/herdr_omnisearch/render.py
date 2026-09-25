from .textmatch import clean_text, shorten
from .live_index import is_machine_row, is_workspace_row, machine_name

def display_status(row) -> str:
    if is_machine_row(row):
        return "machine"
    if is_workspace_row(row):
        return "workspace"
    return row.get("agent_status") or "unknown"


def tree_indent(row) -> str:
    return "  " * int(row.get("_tree_depth") or 0)


def machine_prefix(row) -> str:
    """Name the machine on rows shown outside their machine's subtree."""
    return f"{machine_name(row)} › " if row.get("_top_match") else ""


def format_result(row, *, multiline=False):
    status = display_status(row)
    workspace = row.get("workspace_label") or row.get("workspace_id")
    is_workspace = is_workspace_row(row)
    label = row.get("pane_label") or row.get("pane_id")
    agent = row.get("agent") or "shell"
    cwd = row.get("cwd") or ""
    snippet_value = row.get("snippet")
    if snippet_value is None:
        snippet_value = row.get("content") or ""
    snippet = clean_text(snippet_value or "")
    snippet = " ".join(snippet.split())
    if is_machine_row(row):
        title = f"[machine] {machine_name(row)}"
    elif is_workspace:
        title = f"{tree_indent(row)}[{status}] {workspace}"
    else:
        title = f"{tree_indent(row)}[{status}] {machine_prefix(row)}{workspace} / {agent} / {label}"
    if multiline:
        return (
            f"{row['stable_id']}\t{title}\n"
            f"  {cwd}\n"
            f"  {snippet[:500]}"
        )
    matches = row.get("match_count")
    count = f" x{matches}" if matches and int(matches) > 1 else ""
    left = f"{title}{count}"
    return "\t".join(
        [
            row["stable_id"],
            shorten(left, 56),
            shorten(cwd, 58),
            shorten(snippet, 110),
        ]
    )
