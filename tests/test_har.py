import base64
import json

from vccflow.har import REDACTED, leftover_secrets, normalize_path, process, sanitize_har, summarize

JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyMTIzNDU2In0.c2lnbmF0dXJlMTIzNDU2Nzg"
PLAN_ID = "009309cf-9d61-4cdf-96eb-ebe58804f7ef"


def entry(method, url, *, req_headers=(), post=None, resp_text="", mime="application/json", encoding=None):
    content = {"mimeType": mime, "text": resp_text}
    if encoding:
        content["encoding"] = encoding
    request = {
        "method": method,
        "url": url,
        "headers": [{"name": n, "value": v} for n, v in req_headers],
        "queryString": [],
        "cookies": [{"name": "sid", "value": "abc"}],
    }
    if post is not None:
        request["postData"] = {"mimeType": "application/json", "text": post}
    return {
        "request": request,
        "response": {
            "status": 200,
            "headers": [{"name": "Set-Cookie", "value": "sid=abc"}],
            "cookies": [{"name": "sid", "value": "abc"}],
            "content": content,
        },
    }


def har_of(*entries):
    return {"log": {"version": "1.2", "entries": list(entries)}}


def test_headers_cookies_and_tokens_are_removed():
    har = har_of(entry(
        "PUT", f"https://emea.cc.vonage.com/architect/plans/{PLAN_ID}?access_token=xyz&view=full",
        req_headers=[("Authorization", f"Bearer {JWT}"), ("Cookie", "sid=abc"), ("Accept", "application/json")],
        post=json.dumps({"name": "Plan", "applets": [{"type": "Announcer"}], "csrfToken": "zzz"}),
        resp_text=json.dumps({"access_token": "secret-value", "note": f"Bearer {JWT}"}),
    ))
    clean = sanitize_har(har)
    e = clean["log"]["entries"][0]
    headers = {h["name"]: h["value"] for h in e["request"]["headers"]}
    assert headers["Authorization"] == REDACTED
    assert headers["Cookie"] == REDACTED
    assert headers["Accept"] == "application/json"
    assert e["request"]["cookies"] == [] and e["response"]["cookies"] == []
    assert e["response"]["headers"][0]["value"] == REDACTED
    assert "access_token=" + REDACTED in e["request"]["url"] and "view=full" in e["request"]["url"]

    body = json.loads(e["request"]["postData"]["text"])
    assert body["csrfToken"] == REDACTED
    assert body["applets"] == [{"type": "Announcer"}]  # plan content is kept

    resp = json.loads(e["response"]["content"]["text"])
    assert resp["access_token"] == REDACTED
    assert JWT not in resp["note"]
    assert leftover_secrets(clean) == []


def test_redirect_url_fragment_tokens_are_removed():
    e = entry("GET", "https://emea.newvoicemedia.com/Auth/connect/authorize")
    e["response"]["redirectURL"] = f"https://app/SilentRenew#id_token={JWT}&state=abc&session_state=xyz"
    clean = sanitize_har(har_of(e))
    url = clean["log"]["entries"][0]["response"]["redirectURL"]
    assert JWT not in url and "state=abc" in url
    assert leftover_secrets(clean) == []


def test_static_assets_dropped_and_base64_json_decoded():
    payload = base64.b64encode(json.dumps({"applets": []}).encode()).decode()
    har = har_of(
        entry("GET", "https://emea.cc.vonage.com/app/main.js", mime="application/javascript"),
        entry("GET", "https://emea.cc.vonage.com/img/logo.png", mime="image/png"),
        entry("GET", "https://emea.cc.vonage.com/api/plan", resp_text=payload, encoding="base64"),
    )
    clean = sanitize_har(har)
    assert len(clean["log"]["entries"]) == 1
    content = clean["log"]["entries"][0]["response"]["content"]
    assert json.loads(content["text"]) == {"applets": []}
    assert "encoding" not in content


def test_summary_marks_writes_and_normalizes_ids():
    har = har_of(
        entry("GET", f"https://h/api/plans/{PLAN_ID}"),
        entry("POST", "https://h/api/plans/123456/applets", post="{}"),
    )
    calls = summarize(har)
    assert [c.path for c in calls] == ["/api/plans/{id}", "/api/plans/{n}/applets"]
    assert [c.is_write for c in calls] == [False, True]
    assert normalize_path("/a/2024") == "/a/{n}"


def test_process_writes_clean_file_and_bodies(tmp_path):
    src = tmp_path / "architect.har"
    src.write_text(json.dumps(har_of(
        entry("POST", "https://h/api/plans", post=json.dumps({"name": "x"}), resp_text=json.dumps({"id": 1})),
    )))
    clean_path, bodies_dir, calls, leftovers, n = process(src)
    assert clean_path.name == "architect.clean.har" and clean_path.exists()
    assert n == 2
    assert sorted(p.name for p in bodies_dir.iterdir()) == [
        "0001_POST_api_plans.req.json", "0001_POST_api_plans.resp.json",
    ]
    assert len(calls) == 1 and leftovers == []
