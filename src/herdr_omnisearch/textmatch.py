import io
import re
from datetime import datetime

from .settings import app_config

TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]*")


def clean_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", value)
    value = "".join(ch for ch in value if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    return value.strip()


def tokens(value: str):
    out = []
    for token in TOKEN_RE.findall(value or ""):
        token = token.lower().strip("._:/@+-")
        if 2 <= len(token) <= 64:
            out.append(token)
    return out


def token_trigrams(token: str):
    if len(token) <= 3:
        return {token}
    return {token[i : i + 3] for i in range(len(token) - 2)}


def prefix_upper_bound(prefix: str):
    if not prefix:
        return None
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def edit_distance_limited(left: str, right: str, limit: int):
    if abs(len(left) - len(right)) > limit:
        return None
    if left == right:
        return 0
    if not left:
        return len(right) if len(right) <= limit else None
    if not right:
        return len(left) if len(left) <= limit else None

    previous = list(range(len(right) + 1))
    for i, left_ch in enumerate(left, 1):
        current = [i]
        row_min = current[0]
        for j, right_ch in enumerate(right, 1):
            cost = 0 if left_ch == right_ch else 1
            value = min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + cost,
            )
            current.append(value)
            if value < row_min:
                row_min = value
        if row_min > limit:
            return None
        previous = current
    distance = previous[-1]
    return distance if distance <= limit else None


def fuzzy_distance_limit(term: str) -> int:
    length = len(term)
    if length <= 3:
        return 0
    if length <= 4:
        return 1
    if length <= 7:
        return 2
    if length <= 12:
        return 3
    return 4


def fuzzy_score_threshold(term: str) -> float:
    length = len(term)
    if length <= 4:
        return 0.78
    if length <= 7:
        return 0.75
    if length <= 12:
        return 0.68
    return 0.66


def score_token_candidate(term: str, token: str) -> float:
    if term == token:
        return 1.0
    if token.startswith(term):
        return 0.96
    if len(term) >= 3 and term.startswith(token):
        return 0.88
    if len(term) < 4:
        return 0.0

    max_distance = fuzzy_distance_limit(term)
    distance = edit_distance_limited(term, token, max_distance)
    if distance is None:
        return 0.0
    return max(0.0, 1.0 - (distance / (max(len(term), len(token)) + 1)))


def candidate_tokens_for_term(conn, term: str, *, limit: int = 180):
    term = term.lower()
    candidates = set()
    terms_table, trigrams_table = "terms", "token_trigrams"

    upper = prefix_upper_bound(term)
    if upper:
        rows = conn.execute(
            f"""
            SELECT token
            FROM {terms_table}
            WHERE token = ?
               OR (token >= ? AND token < ?)
            ORDER BY length(token) ASC, token ASC
            LIMIT ?
            """,
            (term, term, upper, limit),
        ).fetchall()
        candidates.update(row["token"] for row in rows)

    if len(term) >= 4:
        grams = sorted(token_trigrams(term))
        overlap_floor = max(1, min(len(grams), len(grams) - fuzzy_distance_limit(term)))
        placeholders = ",".join("?" for _ in grams)
        rows = conn.execute(
            f"""
            SELECT token, COUNT(*) AS overlap
            FROM {trigrams_table}
            WHERE trigram IN ({placeholders})
            GROUP BY token
            HAVING overlap >= ?
            ORDER BY overlap DESC, abs(length(token) - ?) ASC, length(token) ASC
            LIMIT ?
            """,
            (*grams, overlap_floor, len(term), limit * 2),
        ).fetchall()
        candidates.update(row["token"] for row in rows)

    scored = []
    threshold = fuzzy_score_threshold(term)
    for token in candidates:
        score = score_token_candidate(term, token)
        if score >= threshold:
            scored.append((token, score))
    scored.sort(key=lambda item: (-item[1], abs(len(item[0]) - len(term)), item[0]))
    return scored[:limit]


def has_prefix_token(conn, term: str) -> bool:
    upper = prefix_upper_bound(term)
    if not upper:
        return False
    terms_table = "terms"
    return (
        conn.execute(
            f"""
            SELECT 1
            FROM {terms_table}
            WHERE token >= ? AND token < ?
            LIMIT 1
            """,
            (term, upper),
        ).fetchone()
        is not None
    )


def label_match_score(label: str, query_terms) -> int:
    label_terms = set(tokens(label or ""))
    if not label_terms or not query_terms:
        return 0
    score = 0
    for term in query_terms:
        if any(score_token_candidate(term, label_term) >= fuzzy_score_threshold(term) for label_term in label_terms):
            score += 1
    return score


def mark_space_matches(rows, query: str):
    query_terms = list(dict.fromkeys(tokens(query)))
    if not query_terms:
        return
    for row in rows:
        label = row.get("workspace_label") or row.get("space_label") or ""
        score = label_match_score(label, query_terms)
        if score:
            row["_space_match_score"] = score


def space_sort_weight(row) -> int:
    return -int(row.get("_space_match_score") or 0)


def chunk_text(text: str, *, max_lines: int = 55, overlap: int = 6):
    return list(iter_text_chunks(text, max_lines=max_lines, overlap=overlap))


def iter_text_chunks(text: str, *, max_lines: int = 55, overlap: int = 6):
    max_lines = max(1, int(max_lines))
    overlap = max(0, min(int(overlap), max_lines - 1))
    window = []
    emitted = False
    added_after_emit = 0
    for raw_line in io.StringIO(clean_text(text)):
        line = raw_line.rstrip()
        if not line.strip():
            continue
        window.append(line)
        added_after_emit += 1
        if len(window) < max_lines:
            continue
        yield "\n".join(window)
        emitted = True
        window = window[-overlap:] if overlap else []
        added_after_emit = 0
    if window and (not emitted or added_after_emit):
        yield "\n".join(window)


def clip_text(value: str, limit: int = 5000) -> str:
    value = clean_text(value)
    if len(value) <= limit:
        return value
    return value[:limit] + "..."


def title_from_text(text: str, fallback: str) -> str:
    text = " ".join(clean_text(text).split())
    if not text:
        return fallback
    return shorten(text, 80)


def iso_to_epoch(value: str) -> float:
    if not value:
        return 0.0
    try:
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return 0.0


def parse_filters(query: str):
    filters = {}

    def pull(name):
        nonlocal query
        values = re.findall(rf"\b{name}:([A-Za-z0-9_.:/@+-]+)", query)
        if values:
            filters[name] = values[-1]
            query = re.sub(rf"\b{name}:[A-Za-z0-9_.:/@+-]+", " ", query)

    for key in ("status", "agent", "workspace", "cwd", "machine"):
        pull(key)
    return " ".join(query.split()), filters


def fts_query(query: str, *, prefix_min_chars: int = 2) -> str:
    terms = re.findall(r"[\w./@+-]+", query.lower())
    terms = [term.strip(".+-/") for term in terms]
    terms = [term for term in terms if term]
    quoted = []
    for term in terms:
        escaped = term.replace('"', '""')
        if len(term) >= prefix_min_chars:
            quoted.append(f'"{escaped}"*')
        else:
            quoted.append(f'"{escaped}"')
    return " ".join(quoted)


def strip_date_prefix(value: str) -> str:
    value = re.sub(r"^\d{8}-", "", value or "")
    for prefix in app_config()["strip_prefixes"]:
        if value.startswith(prefix):
            value = value[len(prefix) :]
    return value


def shorten_start(value: str, width: int) -> str:
    """Keep the end of a path, where the distinguishing directory usually is."""
    value = " ".join(clean_text(value or "").split())
    if len(value) <= width:
        return value
    if width <= 1:
        return value[-width:] if width else ""
    return "…" + value[-(width - 1):]


def shorten(value: str, width: int) -> str:
    value = " ".join(clean_text(value or "").split())
    if len(value) <= width:
        return value
    if width <= 1:
        return value[:width]
    return value[: width - 1] + "…"
