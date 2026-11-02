"""Summarise how an applet type is created and saved, from a sanitized HAR (read-only).

Usage: python inspect_har.py <file.clean.har> [--full]

Prints each call to the portal; decodes Applet/Save bodies and shows what changed
between successive saves; prints form posts field by field. Values that look like
credentials are masked, because Data Connector and web service settings hold real ones.
--full prints the first saved properties in full (still masked), not only their keys.
"""

from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

SKIP_HOSTS = ("uimonitoring",)
SECRET_KEY = re.compile(r"secret|password|token|bearer|authorization|api_?key", re.I)
SECRET_TEXT = [
    (re.compile(r"(?is)(<Key>[^<]*(?:secret|password|token|authorization|api_?key)[^<]*</Key>\s*<Value>)[^<]*"), r"\1<MASKED>"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"), r"\1<MASKED>"),
    (re.compile(r"(?i)((?:client_secret|password|token|api_?key)[^=:<>\"]{0,20}[=:>]\s*)[^&<\s\"]+"), r"\1<MASKED>"),
]


def mask(value):
    if isinstance(value, dict):
        return {k: "<MASKED>" if SECRET_KEY.search(k) and isinstance(v, str) else mask(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask(v) for v in value]
    if isinstance(value, str):
        for pattern, repl in SECRET_TEXT:
            value = pattern.sub(repl, value)
    return value


def short(value, limit=400) -> str:
    text = json.dumps(mask(value), ensure_ascii=False)
    return text if len(text) <= limit else f"{text[:limit]}...(+{len(text) - limit})"


def main(path: str, full: bool) -> None:
    entries = json.loads(Path(path).read_text(encoding="utf-8"))["log"]["entries"]
    previous: dict[str, dict] = {}
    for i, e in enumerate(entries, 1):
        req, resp = e["request"], e["response"]
        url = urlsplit(req["url"])
        if req["method"] == "OPTIONS" or any(h in url.path for h in SKIP_HOSTS):
            continue
        print(f"\n#{i} {req['method']} {url.path}{'?' + url.query if url.query else ''} -> {resp['status']}")
        location = next((h["value"] for h in resp.get("headers", []) if h["name"].lower() == "location"), None)
        if location:
            print(f"  redirect: {location[:160]}")
        body = (req.get("postData") or {}).get("text") or ""
        if url.path.endswith("Applet/Save") and body:
            data = json.loads(body)
            props = json.loads(base64.b64decode(data["appletProperties"]))
            name = data["appletName"]
            if name not in previous:
                print(f"  first save of '{name}', type {props.get('AppletType')}, url {props.get('Url')}")
                print(f"  keys: {sorted(props)}")
                if full:
                    print(json.dumps(mask(props), indent=1, ensure_ascii=False))
            else:
                changed = {k: (previous[name].get(k), props.get(k))
                           for k in sorted(set(previous[name]) | set(props)) if previous[name].get(k) != props.get(k)}
                print(f"  changed (old, new): {short(changed, 1200)}")
            previous[name] = props
        elif body.lstrip().startswith("{"):
            print(f"  json: {short(json.loads(body))}")
        elif body and "multipart" not in (req.get("postData") or {}).get("mimeType", ""):
            for k, v in parse_qsl(body, keep_blank_values=True):
                shown = "<MASKED>" if SECRET_KEY.search(k) else short(v)
                print(f"  {k} = {shown}")
        elif body:
            print("  multipart body (file upload)")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--full"]
    if len(args) != 1:
        sys.exit(__doc__)
    main(args[0], "--full" in sys.argv)
