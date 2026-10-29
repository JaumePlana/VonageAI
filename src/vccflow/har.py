"""Sanitize a browser HAR recording and summarize the API calls in it.

Used to discover how the Interaction Plans Architect loads and saves plans.
Removes cookies, auth headers, tokens and secrets; drops static assets;
dumps request/response bodies to files for inspection.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

REDACTED = "[REDACTED]"

SENSITIVE_HEADER = re.compile(r"cookie|authori[sz]ation|token|session|csrf|xsrf|api[-_]?key|secret|auth", re.I)
SENSITIVE_KEY = re.compile(
    r"^((access|refresh|id|session|csrf|xsrf|auth|bearer)[-_]?)?token$"
    r"|password|secret|^api[-_]?key$|^authorization$|^cookie$|^code_verifier$|^assertion$",
    re.I,
)
JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]{16,}", re.I)

STATIC_EXT = re.compile(r"\.(js|mjs|css|map|png|jpe?g|gif|svg|ico|webp|woff2?|ttf|eot)$", re.I)
STATIC_MIME = re.compile(r"^(image|font)/|css|javascript", re.I)
TEXT_MIME = re.compile(r"json|xml|text|javascript|x-www-form-urlencoded", re.I)

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
LONG_NUMBER = re.compile(r"(?<=/)\d{4,}(?=/|$)")


# --- sanitizing ---------------------------------------------------------------

def scrub_text(text: str) -> str:
    """Redact tokens inside free text, and sensitive keys if the text is JSON."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        pass
    else:
        if isinstance(data, (dict, list)):
            return json.dumps(scrub_json(data), ensure_ascii=False)
    text = JWT.sub(REDACTED, text)
    return BEARER.sub(lambda m: m.group(1) + REDACTED, text)


def scrub_json(value):
    if isinstance(value, dict):
        return {
            k: REDACTED if SENSITIVE_KEY.search(k) and isinstance(v, (str, int)) else scrub_json(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [scrub_json(v) for v in value]
    if isinstance(value, str):
        return BEARER.sub(lambda m: m.group(1) + REDACTED, JWT.sub(REDACTED, value))
    return value


def _scrub_pairs(pairs: list[dict], pattern: re.Pattern) -> list[dict]:
    return [
        {**p, "value": REDACTED} if pattern.search(p.get("name", "")) else {**p, "value": scrub_text(p.get("value", ""))}
        for p in pairs
    ]


def _scrub_params(params: str) -> str:
    return "&".join(
        f"{k}={REDACTED}" if SENSITIVE_KEY.search(k) else f"{k}={JWT.sub(REDACTED, v)}" if sep else k
        for k, sep, v in (q.partition("=") for q in params.split("&"))
    )


def _scrub_url(url: str) -> str:
    # OIDC redirects carry tokens in the fragment (#id_token=...), not only the query.
    parts = urlsplit(url)
    if not parts.query and not parts.fragment:
        return url
    return parts._replace(query=_scrub_params(parts.query), fragment=_scrub_params(parts.fragment)).geturl()


def decoded_response_text(content: dict) -> str | None:
    text = content.get("text")
    if text is None:
        return None
    if content.get("encoding") == "base64":
        if not TEXT_MIME.search(content.get("mimeType", "")):
            return None
        try:
            return base64.b64decode(text).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
    return text


def is_static(entry: dict) -> bool:
    path = urlsplit(entry["request"]["url"]).path
    mime = entry.get("response", {}).get("content", {}).get("mimeType", "")
    return bool(STATIC_EXT.search(path) or STATIC_MIME.search(mime))


def sanitize_entry(entry: dict) -> dict:
    req = dict(entry["request"])
    req["url"] = _scrub_url(req["url"])
    req["headers"] = _scrub_pairs(req.get("headers", []), SENSITIVE_HEADER)
    req["queryString"] = _scrub_pairs(req.get("queryString", []), SENSITIVE_KEY)
    req["cookies"] = []
    if "postData" in req:
        post = dict(req["postData"])
        if "text" in post:
            post["text"] = scrub_text(post["text"])
        if "params" in post:
            post["params"] = _scrub_pairs(post["params"], SENSITIVE_KEY)
        req["postData"] = post

    resp = dict(entry.get("response", {}))
    if resp.get("redirectURL"):
        resp["redirectURL"] = _scrub_url(resp["redirectURL"])
    resp["headers"] = _scrub_pairs(resp.get("headers", []), SENSITIVE_HEADER)
    resp["cookies"] = []
    content = dict(resp.get("content", {}))
    text = decoded_response_text(content)
    if text is not None:
        content["text"] = scrub_text(text)
        content.pop("encoding", None)
    else:
        content.pop("text", None)  # binary: not needed for API discovery
    resp["content"] = content

    return {**entry, "request": req, "response": resp}


def sanitize_har(har: dict, keep_static: bool = False) -> dict:
    log = dict(har["log"])
    log["entries"] = [
        sanitize_entry(e) for e in log.get("entries", []) if keep_static or not is_static(e)
    ]
    return {**har, "log": log}


def leftover_secrets(har: dict) -> list[str]:
    """Return a few suspicious strings still present after sanitizing."""
    text = json.dumps(har, ensure_ascii=False)
    return sorted({m.group(0)[:40] + "..." for m in JWT.finditer(text)} | {
        m.group(0)[:40] + "..." for m in BEARER.finditer(text) if REDACTED not in m.group(0)
    })[:10]


# --- summarizing --------------------------------------------------------------

@dataclass
class Call:
    index: int
    method: str
    host: str
    path: str
    status: int
    mime: str
    request_bytes: int
    response_bytes: int

    @property
    def is_write(self) -> bool:
        return self.method in ("POST", "PUT", "PATCH", "DELETE")


def normalize_path(path: str) -> str:
    return LONG_NUMBER.sub("{n}", UUID.sub("{id}", path))


def summarize(har: dict) -> list[Call]:
    calls = []
    for i, e in enumerate(har["log"].get("entries", []), start=1):
        req, resp = e["request"], e.get("response", {})
        url = urlsplit(req["url"])
        content = resp.get("content", {})
        calls.append(Call(
            index=i,
            method=req["method"],
            host=url.netloc,
            path=normalize_path(url.path),
            status=resp.get("status", 0),
            mime=content.get("mimeType", "").split(";")[0],
            request_bytes=len(req.get("postData", {}).get("text", "") or ""),
            response_bytes=len(content.get("text", "") or ""),
        ))
    return calls


def format_summary(calls: list[Call]) -> str:
    lines = [f"{len(calls)} requests ({sum(c.is_write for c in calls)} writes)", ""]
    for c in calls:
        mark = "*" if c.is_write else " "
        lines.append(
            f"{mark}{c.index:4} {c.method:6} {c.status:3} {c.host}{c.path}  "
            f"[{c.mime or '-'} req={c.request_bytes} resp={c.response_bytes}]"
        )
    lines += ["", "* = write (POST/PUT/PATCH/DELETE)"]
    return "\n".join(lines)


# --- body dump ----------------------------------------------------------------

def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")[:80]


def _pretty(text: str) -> tuple[str, str]:
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False), "json"
    except (ValueError, TypeError):
        return text, "txt"


def dump_bodies(har: dict, out_dir: Path) -> int:
    """Write each non-empty request/response body to out_dir; return the file count."""
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for c, e in zip(summarize(har), har["log"].get("entries", [])):
        stem = f"{c.index:04}_{c.method}_{_safe_name(c.path)}"
        bodies = {
            "req": e["request"].get("postData", {}).get("text"),
            "resp": e.get("response", {}).get("content", {}).get("text"),
        }
        for kind, text in bodies.items():
            if text:
                body, ext = _pretty(text)
                (out_dir / f"{stem}.{kind}.{ext}").write_text(body, encoding="utf-8")
                count += 1
    return count


def process(har_path: Path, keep_static: bool = False) -> tuple[Path, Path, list[Call], list[str], int]:
    """Sanitize har_path, write <name>.clean.har and <name>.bodies/ next to it."""
    har = json.loads(har_path.read_text(encoding="utf-8-sig"))
    clean = sanitize_har(har, keep_static=keep_static)
    clean_path = har_path.with_name(har_path.stem + ".clean.har")
    clean_path.write_text(json.dumps(clean, indent=2, ensure_ascii=False), encoding="utf-8")
    bodies_dir = har_path.with_name(har_path.stem + ".bodies")
    n_bodies = dump_bodies(clean, bodies_dir)
    return clean_path, bodies_dir, summarize(clean), leftover_secrets(clean), n_bodies
