import hashlib
import re
import sqlite3
import time
from pathlib import Path

from .herdr_cli import HerdrCLI
from .herdr_socket import HerdrClient, HerdrError, socket_is_alive
from .settings import (
    app_config,
    cli_command,
    data_dir,
    herdr_bin,
    herdr_session_identity,
    herdr_session_key,
)
from .storage import (
    connect,
    lock_is_held,
    spawn_locked_background,
    stop_watcher_at,
    try_exclusive_lock,
)
from .textmatch import (
    candidate_tokens_for_term,
    chunk_text,
    clean_text,
    fts_query,
    has_prefix_token,
    mark_space_matches,
    parse_filters,
    space_sort_weight,
    strip_date_prefix,
    token_trigrams,
    tokens,
)

STATUS_WEIGHT = {
    "workspace": 0,
    "working": 0,
    "blocked": 1,
    "idle": 2,
    "done": 3,
    "unknown": 4,
}


def pane_agent_session_id(pane) -> str:
    session = pane.get("agent_session") or {}
    if isinstance(session, dict):
        return session.get("value") or ""
    return ""


def pane_agent(pane) -> str:
    session = pane.get("agent_session") or {}
    if isinstance(session, dict) and session.get("agent"):
        return session["agent"]
    return pane.get("agent") or ""


def merge_agent_records(panes, agents):
    agents_by_pane = {
        agent.get("pane_id"): agent
        for agent in agents
        if isinstance(agent, dict) and agent.get("pane_id")
    }
    merged = []
    for pane in panes:
        record = dict(pane)
        live_agent = agents_by_pane.get(pane.get("pane_id"))
        if live_agent:
            record.update(live_agent)
        record["agent"] = pane_agent(record)
        merged.append(record)
    return merged


def metadata_text(pane, workspace_label: str) -> str:
    agent_session_id = pane_agent_session_id(pane)
    fields = [
        f"workspace {workspace_label}",
        f"workspace_id {pane.get('workspace_id', '')}",
        f"tab_id {pane.get('tab_id', '')}",
        f"pane_id {pane.get('pane_id', '')}",
        f"terminal_id {pane.get('terminal_id', '')}",
        f"label {pane.get('label', '')}",
        f"agent {pane_agent(pane)}",
        f"agent_session {agent_session_id}",
        f"status {pane.get('agent_status', '')}",
        f"cwd {pane.get('cwd', '')}",
        f"foreground_cwd {pane.get('foreground_cwd', '')}",
    ]
    return "\n".join(field for field in fields if field.strip())


def workspace_metadata_text(workspace) -> str:
    fields = [
        "type workspace",
        f"workspace {workspace.get('label') or workspace.get('workspace_id', '')}",
        f"workspace_id {workspace.get('workspace_id', '')}",
        f"tab_id {workspace.get('active_tab_id', '')}",
        f"pane_count {workspace.get('pane_count', '')}",
        f"tab_count {workspace.get('tab_count', '')}",
    ]
    return "\n".join(field for field in fields if field.strip())


def pane_recent_text(client: HerdrClient, pane, lines: int) -> str:
    # Not `herdr agent read`: it harvests history by scrolling the user's pane.
    pane_id = pane.get("pane_id") or ""
    try:
        return client.pane_read(pane_id, lines)
    except HerdrError as exc:
        return f"[pane read failed] {exc}"


def should_skip_pane(pane, workspace_label: str, *, include_wrappers: bool) -> bool:
    if include_wrappers:
        return False
    cfg = app_config()
    label = (pane.get("label") or "").lower()
    workspace = (workspace_label or "").lower()
    cwd = (pane.get("cwd") or "").lower()
    status = pane.get("agent_status") or "unknown"
    agent = pane.get("agent") or ""
    if any(needle.lower() in label for needle in cfg["skip_label_contains"]):
        return True
    if any(workspace == pair[0].lower() and cwd == pair[1].lower() for pair in cfg["skip_workspace_cwd_pairs"]):
        return True
    if cfg["skip_unknown_without_agent"] and status == "unknown" and not agent:
        return True
    return False


def index_session(lines: int, include_empty: bool, include_wrappers: bool, snapshot=None) -> int:
    client = HerdrClient()
    agent_cli = HerdrCLI(herdr_bin())
    snapshot = snapshot or client.snapshot()
    workspaces_payload = snapshot.get("workspaces", [])
    workspace_labels = {
        workspace["workspace_id"]: workspace.get("label") or workspace["workspace_id"]
        for workspace in workspaces_payload
    }
    panes = merge_agent_records(snapshot.get("panes", []), agent_cli.agent_list())

    session_key, session_socket = herdr_session_identity()
    now = int(time.time())
    docs = []
    doc_tokens = []
    indexable_panes = []
    indexable_workspace_counts = {}
    for pane in panes:
        workspace_label = workspace_labels.get(pane.get("workspace_id"), pane.get("workspace_id", ""))
        if should_skip_pane(pane, workspace_label, include_wrappers=include_wrappers):
            continue
        indexable_panes.append((pane, workspace_label))
        workspace_id = pane.get("workspace_id", "")
        indexable_workspace_counts[workspace_id] = indexable_workspace_counts.get(workspace_id, 0) + 1

    for workspace in workspaces_payload:
        workspace_id = workspace.get("workspace_id", "")
        if not include_wrappers and indexable_workspace_counts.get(workspace_id, 0) == 0:
            continue
        workspace_label = workspace.get("label") or workspace_id
        body = workspace_metadata_text(workspace)
        digest = hashlib.sha1(
            f"{session_key}\0workspace\0{workspace_id}\0{body}".encode("utf-8", "replace")
        ).hexdigest()
        docs.append(
            {
                "stable_id": digest,
                "herdr_session": session_key,
                "socket_path": session_socket,
                "workspace_id": workspace_id,
                "workspace_label": workspace_label,
                "tab_id": workspace.get("active_tab_id", ""),
                "pane_id": f"workspace:{workspace_id}",
                "terminal_id": "",
                "pane_label": workspace_label,
                "agent": "",
                "agent_session_id": "",
                "agent_status": "workspace",
                "cwd": "",
                "foreground_cwd": "",
                "chunk_index": 0,
                "content": body,
                "body": body,
                "indexed_at": now,
            }
        )
        for token in set(tokens(body)):
            doc_tokens.append((token, digest))

    for pane, workspace_label in indexable_panes:
        pane_id = pane["pane_id"]
        agent_session_id = pane_agent_session_id(pane)
        meta = metadata_text(pane, workspace_label)
        recent = pane_recent_text(client, pane, lines)
        chunks = chunk_text(recent)
        if include_empty or not chunks:
            chunks = chunks or ["[empty pane]\n" + meta]
        for idx, chunk in enumerate(chunks):
            body = f"{meta}\n\n{chunk}"
            digest = hashlib.sha1(
                f"{session_key}\0{pane_id}\0{idx}\0{body}".encode("utf-8", "replace")
            ).hexdigest()
            docs.append(
                {
                    "stable_id": digest,
                    "herdr_session": session_key,
                    "socket_path": session_socket,
                    "workspace_id": pane.get("workspace_id", ""),
                    "workspace_label": workspace_label,
                    "tab_id": pane.get("tab_id", ""),
                    "pane_id": pane_id,
                    "terminal_id": pane.get("terminal_id", ""),
                    "pane_label": pane.get("label", ""),
                    "agent": pane_agent(pane),
                    "agent_session_id": agent_session_id,
                    "agent_status": pane.get("agent_status", ""),
                    "cwd": pane.get("cwd", ""),
                    "foreground_cwd": pane.get("foreground_cwd", ""),
                    "chunk_index": idx,
                    "content": chunk,
                    "body": body,
                    "indexed_at": now,
                }
            )
            indexed_tokens = set(tokens(body))
            for token in indexed_tokens:
                doc_tokens.append((token, digest))

    conn = connect()
    with conn:
        # Replace only this session's rows so concurrent Herdr sessions on the
        # same machine never clobber each other. Rows without a session are
        # pre-upgrade leftovers and are swept by whichever session runs first.
        replace_docs(
            conn,
            "machine_id = '' AND (herdr_session = :session OR herdr_session IS NULL)",
            {"session": session_key},
            docs,
            doc_tokens,
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_indexed_at', ?)",
            (str(now),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (f"last_indexed_at:{session_key}", str(now)),
        )
        reap_dead_sessions(conn, session_key)
    conn.close()
    return len(docs)


DOC_COLUMNS = (
    "stable_id", "herdr_session", "socket_path", "machine_id", "machine_label",
    "workspace_id", "workspace_label", "tab_id", "terminal_id",
    "pane_id", "pane_label", "agent", "agent_session_id", "agent_status", "cwd",
    "foreground_cwd", "chunk_index", "content", "indexed_at",
)


def replace_docs(conn, owner_where: str, owner_params, docs, doc_tokens) -> None:
    """Swap the rows matching owner_where for docs, keeping FTS and token tables in sync."""
    stale = f"SELECT stable_id FROM docs WHERE {owner_where}"
    conn.execute(f"DELETE FROM docs_fts WHERE stable_id IN ({stale})", owner_params)
    conn.execute(f"DELETE FROM token_docs WHERE stable_id IN ({stale})", owner_params)
    conn.execute(f"DELETE FROM docs WHERE {owner_where}", owner_params)
    rows = [
        {"machine_id": "", "machine_label": None, **doc}
        for doc in docs
    ]
    conn.executemany(
        f"""
        INSERT INTO docs ({', '.join(DOC_COLUMNS)})
        VALUES ({', '.join(':' + column for column in DOC_COLUMNS)})
        """,
        rows,
    )
    conn.executemany(
        "INSERT INTO docs_fts (stable_id, body) VALUES (:stable_id, :body)",
        rows,
    )
    if not doc_tokens:
        return
    unique_terms = sorted({token for token, _stable_id in doc_tokens})
    conn.executemany(
        "INSERT OR IGNORE INTO terms (token) VALUES (?)",
        [(token,) for token in unique_terms],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO token_docs (token, stable_id) VALUES (?, ?)",
        doc_tokens,
    )
    conn.executemany(
        "INSERT OR IGNORE INTO token_trigrams (trigram, token) VALUES (?, ?)",
        [(trigram, token) for token in unique_terms for trigram in token_trigrams(token)],
    )


def reap_dead_sessions(conn, current_key: str) -> int:
    """Drop index rows and state files of sessions whose socket is gone."""
    rows = conn.execute(
        """
        SELECT DISTINCT herdr_session, socket_path FROM docs
        WHERE machine_id = '' AND herdr_session IS NOT NULL AND herdr_session != ?
        """,
        (current_key,),
    ).fetchall()
    dead = [
        row["herdr_session"]
        for row in rows
        if not socket_is_alive(row["socket_path"] or "")
    ]
    for key in dead:
        replace_docs(conn, "machine_id = '' AND herdr_session = :session", {"session": key}, [], [])
        conn.execute("DELETE FROM meta WHERE key = ?", (f"last_indexed_at:{key}",))
        stop_watcher_at(data_dir() / f"watch-{key}.pid")
        for leftover in (
            data_dir() / f"watch-{key}.pid",
            data_dir() / f"watch-{key}.log",
            data_dir() / f"index-{key}.lock",
        ):
            if lock_is_held(leftover):
                continue
            try:
                leftover.unlink()
            except OSError:
                pass
    return len(dead)


def maybe_background_index(lines: int, include_empty: bool, include_wrappers: bool, stale_seconds: int):
    session_key = herdr_session_key()
    conn = connect()
    try:
        doc_count = conn.execute(
            "SELECT COUNT(*) FROM docs WHERE machine_id = '' AND herdr_session = ?", (session_key,)
        ).fetchone()[0]
        last = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (f"last_indexed_at:{session_key}",)
        ).fetchone()
    finally:
        conn.close()

    last_indexed = int(last[0]) if last else 0
    if doc_count and int(time.time()) - last_indexed < stale_seconds:
        return

    lock_fd = try_exclusive_lock(data_dir() / f"index-{session_key}.lock")
    if lock_fd is None:
        return

    cmd = [*cli_command(), "index", "--lines", str(lines)]
    if include_empty:
        cmd.append("--include-empty")
    if include_wrappers:
        cmd.append("--include-wrappers")
    spawn_locked_background(cmd, lock_fd)


def scope_clause(all_sessions: bool, machines: bool, params) -> str:
    """Rows visible to a search: this session, all local sessions, and optionally synced machines."""
    if all_sessions:
        return "1 = 1" if machines else "d.machine_id = ''"
    params["herdr_session"] = herdr_session_key()
    local = "(d.machine_id = '' AND d.herdr_session = :herdr_session)"
    return f"(d.machine_id <> '' OR {local})" if machines else local


def search_index(
    query: str,
    limit: int,
    *,
    status=None,
    agent=None,
    snippets=True,
    all_sessions=False,
    machines=False,
    machine_id=None,
):
    query, filters = parse_filters(query)
    if status:
        filters["status"] = status
    if agent:
        filters["agent"] = agent
    query_terms = tokens(query)

    params = {}
    clauses = [scope_clause(all_sessions, machines, params)]
    if machine_id is not None:
        clauses.append("d.machine_id = :machine_id")
        params["machine_id"] = machine_id
    if filters.get("status"):
        clauses.append("COALESCE(d.agent_status, '') = :status")
        params["status"] = filters["status"]
    if filters.get("agent"):
        clauses.append("COALESCE(d.agent, '') = :agent")
        params["agent"] = filters["agent"]
    if filters.get("workspace"):
        clauses.append("COALESCE(d.workspace_label, '') LIKE :workspace")
        params["workspace"] = f"%{filters['workspace']}%"
    if filters.get("cwd"):
        clauses.append("COALESCE(d.cwd, '') LIKE :cwd")
        params["cwd"] = f"%{filters['cwd']}%"
    if filters.get("machine"):
        clauses.append("COALESCE(NULLIF(d.machine_label, ''), 'Local') LIKE :machine")
        params["machine"] = f"%{filters['machine']}%"

    conn = connect()
    fts = fts_query(query)
    rows = []
    if fts:
        try:
            where = ["docs_fts MATCH :fts"]
            where.extend(clauses)
            fts_params = dict(params)
            fts_params["fts"] = fts
            fts_params["limit"] = limit
            sql = f"""
                SELECT d.*, bm25(docs_fts) AS rank,
                       substr(d.content, 1, 260) AS snippet
                FROM docs_fts
                JOIN docs d ON d.stable_id = docs_fts.stable_id
                WHERE {' AND '.join(where)}
                ORDER BY rank ASC
                LIMIT :limit
            """
            rows = [dict(row) for row in conn.execute(sql, fts_params).fetchall()]
            if snippets:
                for row in rows:
                    row["snippet"] = fuzzy_snippet(row.get("content") or "", query_terms)
        except sqlite3.OperationalError:
            rows = []
    needs_typo_fuzzy = any(
        len(term) >= 4 and not has_prefix_token(conn, term)
        for term in query_terms
    )
    needs_more_results = bool(query_terms) and len(rows) < min(limit, 40)
    use_fuzzy = (not fts) or (not rows) or needs_typo_fuzzy or needs_more_results
    if use_fuzzy:
        fuzzy_rows = fuzzy_search(conn, query, clauses, params, limit * 4, snippets=snippets)
        seen = {row["stable_id"]: row for row in rows}
        for row in fuzzy_rows:
            existing = seen.get(row["stable_id"])
            if existing:
                if row.get("matched_tokens"):
                    existing["matched_tokens"] = row["matched_tokens"]
                continue
            if row["stable_id"] not in seen:
                rows.append(row)
                seen[row["stable_id"]] = row
    mark_space_matches(rows, query)
    rows.sort(key=lambda row: (space_sort_weight(row), float(row.get("rank") or 0)))
    conn.close()
    return rows[:limit]


def fuzzy_search(conn, query: str, clauses, params, limit: int, *, snippets=True):
    query_terms = list(dict.fromkeys(tokens(query)))
    if not query_terms:
        where = clauses or ["1 = 1"]
        sql = f"""
            SELECT d.*, 0.0 AS rank, substr(d.content, 1, 260) AS snippet
            FROM docs d
            WHERE {' AND '.join(where)}
            ORDER BY d.indexed_at DESC, d.workspace_label ASC, d.chunk_index ASC
            LIMIT :limit
        """
        fuzzy_params = dict(params)
        fuzzy_params["limit"] = limit
        return [dict(row) for row in conn.execute(sql, fuzzy_params).fetchall()]

    per_term_matches = []
    for term in query_terms:
        token_scores = dict(candidate_tokens_for_term(conn, term))
        if not token_scores:
            return []
        placeholders = ",".join("?" for _ in token_scores)
        rows = conn.execute(
            f"""
            SELECT token, stable_id
            FROM token_docs
            WHERE token IN ({placeholders})
            """,
            tuple(token_scores),
        ).fetchall()
        stable_matches = {}
        for token, stable_id in rows:
            score = token_scores[token]
            current = stable_matches.get(stable_id)
            if current is None or score > current[0]:
                stable_matches[stable_id] = (score, token)
        if not stable_matches:
            return []
        per_term_matches.append(stable_matches)

    common_ids = set(per_term_matches[0])
    for matches in per_term_matches[1:]:
        common_ids.intersection_update(matches)
        if not common_ids:
            return []

    scored_ids = []
    for stable_id in common_ids:
        scores = [matches[stable_id][0] for matches in per_term_matches]
        matched_tokens = [matches[stable_id][1] for matches in per_term_matches]
        min_score = min(scores)
        avg_score = sum(scores) / len(scores)
        scored_ids.append((stable_id, -((avg_score + min_score) / 2), matched_tokens))
    scored_ids.sort(key=lambda item: item[1])

    fetch_cap = min(len(scored_ids), max(limit * 4, 300), 900)
    selected = scored_ids[:fetch_cap]
    if not selected:
        return []

    id_params = {f"id{idx}": stable_id for idx, (stable_id, _rank, _tokens) in enumerate(selected)}
    id_lookup = {stable_id: (rank, matched_tokens) for stable_id, rank, matched_tokens in selected}
    id_clause = ", ".join(f":{key}" for key in id_params)
    where = [f"d.stable_id IN ({id_clause})"]
    where.extend(clauses)
    sql = f"""
        SELECT d.*
        FROM docs d
        WHERE {' AND '.join(where)}
    """
    fetched = [dict(row) for row in conn.execute(sql, {**params, **id_params}).fetchall()]
    for row in fetched:
        rank, matched_tokens = id_lookup[row["stable_id"]]
        row["rank"] = rank
        row["matched_tokens"] = matched_tokens
        row["snippet"] = fuzzy_snippet(row.get("content") or "", matched_tokens) if snippets else ""
    return sorted(fetched, key=lambda row: row["rank"])[:limit]


def fuzzy_snippet(content: str, matched_tokens):
    text = clean_text(content)
    lower = text.lower()
    positions = [lower.find(token.lower()) for token in matched_tokens if token]
    positions = [pos for pos in positions if pos >= 0]
    if positions:
        start = max(0, min(positions) - 80)
        end = min(len(text), min(positions) + 220)
        snippet = text[start:end]
        if start:
            snippet = "..." + snippet
        if end < len(text):
            snippet += "..."
        return snippet
    return text[:260]


def is_workspace_row(row) -> bool:
    return (row.get("pane_id") or "").startswith("workspace:")


def workspace_key(row):
    return (row.get("machine_id") or "", row.get("herdr_session") or "", row.get("workspace_id") or "")


def fetch_workspace_rows(keys):
    keys = [key for key in dict.fromkeys(keys) if key[2]]
    if not keys:
        return {}
    placeholders = ",".join("?" for _ in keys)
    conn = connect()
    try:
        rows = conn.execute(
            f"""
            SELECT *
            FROM docs
            WHERE pane_id IN ({placeholders})
            """,
            tuple(f"workspace:{key[2]}" for key in keys),
        ).fetchall()
    finally:
        conn.close()
    wanted = set(keys)
    found = {}
    for row in rows:
        row = dict(row)
        key = workspace_key(row)
        if key in wanted:
            found[key] = row
    return found


def machine_name(row) -> str:
    return row.get("machine_label") or "Local"


def machine_header(machine_id: str, rows):
    first = rows[0]
    workspaces = {workspace_key(row) for row in rows}
    return {
        "stable_id": f"machine:{machine_id or 'local'}",
        "machine_id": machine_id,
        "machine_label": first.get("machine_label"),
        "pane_id": f"machine:{machine_id or 'local'}",
        "workspace_id": "",
        "workspace_label": machine_name(first),
        "agent_status": "machine",
        "cwd": "",
        "content": "",
        "match_count": len(workspaces),
    }


def is_machine_row(row) -> bool:
    return (row.get("pane_id") or "").startswith("machine:")


def decorate_live_tree(rows, *, machine_level=False):
    if not rows:
        return rows
    workspace_order = []
    workspace_rows = {}
    child_rows = {}
    for row in rows:
        key = workspace_key(row)
        if key[2] and key not in workspace_order:
            workspace_order.append(key)
        if is_workspace_row(row):
            workspace_rows[key] = row
            continue
        child_rows.setdefault(key, []).append(row)

    missing_headers = [
        key
        for key in workspace_order
        if key not in workspace_rows and child_rows.get(key)
    ]
    workspace_rows.update(fetch_workspace_rows(missing_headers))

    depth = 1 if machine_level else 0
    groups = {}
    machine_order = []
    for key in workspace_order:
        header = workspace_rows.get(key)
        children = child_rows.get(key, [])
        block = []
        if header:
            header = dict(header)
            header["_tree_depth"] = depth
            block.append(header)
        for child in children:
            child = dict(child)
            child["_tree_depth"] = depth + 1
            child["_under_workspace"] = header is not None
            block.append(child)
        if not block:
            continue
        if key[0] not in groups:
            machine_order.append(key[0])
            groups[key[0]] = []
        groups[key[0]].extend(block)

    decorated = []
    if not any(float(row.get("rank") or 0) for row in rows):
        machine_order.sort(key=lambda machine_id: (machine_id != "", machine_name(groups[machine_id][0]).lower()))
    for machine_id in machine_order:
        block = groups[machine_id]
        if machine_level:
            header = machine_header(machine_id, block)
            header["_tree_depth"] = 0
            decorated.append(header)
        decorated.extend(block)
    return decorated


TOP_MATCHES = 5


def synced_machine_ids():
    conn = connect()
    try:
        return [
            row[0]
            for row in conn.execute(
                """
                SELECT machine_id FROM docs WHERE machine_id <> ''
                GROUP BY machine_id ORDER BY MIN(machine_label)
                """
            ).fetchall()
        ]
    finally:
        conn.close()


def grouped_search_index(
    query: str,
    limit: int,
    *,
    status=None,
    agent=None,
    snippets=True,
    all_sessions=False,
    machines=False,
):
    # Each machine gets its own limit so a busy host cannot crowd the others out.
    owners = synced_machine_ids() if machines else []
    rows = []
    for owner in ["", *owners] if owners else [None]:
        rows.extend(
            search_index(
                query,
                max(limit * 8, 120),
                status=status,
                agent=agent,
                snippets=snippets,
                all_sessions=all_sessions,
                machines=machines,
                machine_id=owner,
            )
        )
    grouped = {}
    for row in rows:
        pane_key = (row.get("machine_id") or "", row.get("herdr_session") or "", row["pane_id"])
        current = grouped.get(pane_key)
        if current is None:
            row["match_count"] = 1
            grouped[pane_key] = row
            continue
        current["match_count"] += 1
        if float(row.get("rank") or 0) < float(current.get("rank") or 0):
            row["match_count"] = current["match_count"]
            grouped[pane_key] = row

    def sort_key(row):
        return (
            space_sort_weight(row),
            float(row.get("rank") or 0),
            STATUS_WEIGHT.get(row.get("agent_status") or "unknown", 9),
            row.get("workspace_label") or "",
            row.get("pane_label") or "",
        )

    per_machine = {}
    ranked = []
    for row in sorted(grouped.values(), key=sort_key):
        owner = row.get("machine_id") or ""
        if per_machine.get(owner, 0) >= limit:
            continue
        per_machine[owner] = per_machine.get(owner, 0) + 1
        ranked.append(row)
    spans_machines = len({row.get("machine_id") or "" for row in ranked}) > 1
    machine_level = machines and (spans_machines or any(row.get("machine_id") for row in ranked))
    tree = decorate_live_tree(ranked, machine_level=machine_level)
    # Pinning only adds information when the hits are spread over machines.
    if not (spans_machines and query.strip()):
        return tree
    return top_matches(ranked) + tree


def top_matches(ranked):
    """Best pane hits across every machine, pinned above the per-machine tree."""
    top = []
    for row in ranked:
        if is_workspace_row(row):
            continue
        row = dict(row)
        row["_top_match"] = True
        row["_tree_depth"] = 0
        top.append(row)
        if len(top) >= TOP_MATCHES:
            break
    return top


def derive_space_label_from_cwd(cwd: str) -> str:
    cwd = clean_text(cwd or "")
    if not cwd:
        return "archive"
    exact_label = app_config()["exact_workspace_labels"].get(cwd)
    if exact_label:
        return exact_label
    parts = [part for part in Path(cwd).parts if part not in ("/", "")]
    if not parts:
        return "archive"
    worktree_part = ""
    for marker in app_config()["worktree_markers"]:
        if marker in parts:
            marker_index = parts.index(marker)
            if marker_index + 1 < len(parts):
                worktree_part = parts[marker_index + 1]
                break
    if worktree_part:
        label = strip_date_prefix(worktree_part)
    else:
        label = parts[-1]
    label = label.replace("_", "-").replace("-", " ")
    for word in app_config()["remove_words"]:
        label = re.sub(rf"\b{re.escape(word)}\b", "", label)
    label = " ".join(label.split())
    return label or parts[-1]


def live_space_label_for_session(agent: str, session_id: str, conn=None, cache=None):
    agent = clean_text(agent or "")
    session_id = clean_text(session_id or "")
    if not agent or not session_id:
        return ""
    key = (agent, session_id)
    if cache is not None and key in cache:
        return cache[key]
    row = None
    close_conn = conn is None
    try:
        conn = conn or connect()
        row = conn.execute(
            """
            SELECT workspace_label, COUNT(*) AS count
            FROM docs
            WHERE machine_id = ''
              AND COALESCE(agent, '') = ?
              AND COALESCE(agent_session_id, '') = ?
              AND COALESCE(workspace_label, '') <> ''
            GROUP BY workspace_label
            ORDER BY count DESC, workspace_label ASC
            LIMIT 1
            """,
            (agent, session_id),
        ).fetchone()
    except sqlite3.Error:
        return ""
    finally:
        if close_conn and conn is not None:
            conn.close()
    label = row["workspace_label"] if row else ""
    if cache is not None:
        cache[key] = label
    return label


def live_space_label_for_cwd(cwd: str, conn=None, cache=None):
    cwd = clean_text(cwd or "")
    if not cwd:
        return ""
    if cache is not None and cwd in cache:
        return cache[cwd]
    row = None
    close_conn = conn is None
    try:
        conn = conn or connect()
        row = conn.execute(
            """
            SELECT workspace_label, COUNT(*) AS count
            FROM docs
            WHERE machine_id = ''
              AND cwd = ?
              AND COALESCE(workspace_label, '') <> ''
            GROUP BY workspace_label
            ORDER BY count DESC, workspace_label ASC
            LIMIT 1
            """,
            (cwd,),
        ).fetchone()
    except sqlite3.Error:
        return ""
    finally:
        if close_conn and conn is not None:
            conn.close()
    label = row["workspace_label"] if row else ""
    if cache is not None:
        cache[cwd] = label
    return label


def live_space_labels_by_session(conn):
    rows = conn.execute(
        """
        SELECT agent, agent_session_id, workspace_label, COUNT(*) AS count
        FROM docs
        WHERE machine_id = ''
          AND COALESCE(agent, '') <> ''
          AND COALESCE(agent_session_id, '') <> ''
          AND COALESCE(workspace_label, '') <> ''
        GROUP BY agent, agent_session_id, workspace_label
        ORDER BY agent ASC, agent_session_id ASC, count DESC, workspace_label ASC
        """
    ).fetchall()
    labels = {}
    for row in rows:
        key = (row["agent"], row["agent_session_id"])
        if key not in labels:
            labels[key] = row["workspace_label"]
    return labels


def live_space_labels_by_cwd(conn):
    rows = conn.execute(
        """
        SELECT cwd, workspace_label, COUNT(*) AS count
        FROM docs
        WHERE machine_id = ''
          AND COALESCE(cwd, '') <> ''
          AND COALESCE(workspace_label, '') <> ''
        GROUP BY cwd, workspace_label
        ORDER BY cwd ASC, count DESC, workspace_label ASC
        """
    ).fetchall()
    labels = {}
    for row in rows:
        cwd = clean_text(row["cwd"] or "")
        if cwd and cwd not in labels:
            labels[cwd] = row["workspace_label"]
    return labels
