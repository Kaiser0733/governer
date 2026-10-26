"""Checks for governer. Run: python3 test_governer.py   (pytest also works)."""
import json
import os
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

import governer as g

P = {"m": {"in": 10.0, "out": 50.0}}  # gpt-6-astra shaped
RATE = lambda model: (10 / 1e6, 50 / 1e6)


class FakeUpstream:
    """Stands in for a provider: answers with a fixed usage block."""

    def __init__(self, usage=None, status=200):
        usage = usage or {"prompt_tokens": 2000, "completion_tokens": 100}
        outer = self
        self.hits = 0

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.hits += 1
                self.rfile.read(int(self.headers.get("content-length") or 0))
                out = json.dumps({"choices": [{"message": {"content": "ok"}}],
                                  "usage": usage}).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        self.srv = g.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def stop(self):
        self.srv.shutdown()


def serve(tr, upstream, dry=False, notify_log=None, prices=P):
    cfg = {"upstream": upstream, "rate": RATE, "timeout": 5, "dry_run": dry,
           "prices": prices,
           "alert": (notify_log.append if notify_log is not None else (lambda p: None))}
    srv = g.ThreadingHTTPServer(("127.0.0.1", 0), g.make_handler(tr, cfg))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def post(url, body, agent=None, raw=None):
    data = raw if raw is not None else json.dumps(body).encode()
    req = urllib.request.Request(url + "/v1/chat/completions", data=data, method="POST")
    req.add_header("content-type", "application/json")
    if agent:
        req.add_header("x-governer-agent", agent)
    return urllib.request.urlopen(req, timeout=5)


# ---------------------------------------------------------------- money math

def test_cost_basic():
    assert abs(g.cost("m", {"input": 1_000_000, "output": 1_000_000}, P) - 60.0) < 1e-9


def test_cache_read_is_a_tenth():
    assert abs(g.cost("m", {"cache_read": 1_000_000}, P)
               - g.cost("m", {"input": 1_000_000}, P) * 0.1) < 1e-9


def test_provider_field_spellings_normalize():
    a, inc_a = g.normalize({"prompt_tokens": 5, "completion_tokens": 2, "cached_tokens": 1})
    b, inc_b = g.normalize({"input_tokens": 5, "output_tokens": 2,
                            "cache_read_input_tokens": 1})
    assert a == b == {"input": 5, "output": 2, "cache_read": 1}
    assert inc_a is True and inc_b is False, "OpenAI includes cached tokens; Anthropic does not"


def test_openai_prompt_tokens_are_not_double_billed():
    """prompt_tokens already contains the cached ones — charging both is a 5x overbill."""
    usage = {"input": 1_000_000, "cache_read": 900_000}
    inclusive = g.cost("m", usage, P, inclusive=True)
    exclusive = g.cost("m", usage, P, inclusive=False)
    assert abs(inclusive - 1.9) < 1e-9, inclusive   # 100k fresh @ $10/M + 900k cached @ $1/M
    assert abs(exclusive - 10.9) < 1e-9, exclusive


def test_nested_prompt_tokens_details_are_read():
    usage, inclusive = g.normalize({"prompt_tokens": 10_000,
                                    "prompt_tokens_details": {"cached_tokens": 8_000}})
    assert usage["cache_read"] == 8_000 and inclusive is True


def test_cost_uses_the_table_cache_price():
    table = {"x": {"in": 10.0, "out": 50.0, "cache_read": 2.5}}
    assert abs(g.cost("x", {"cache_read": 1_000_000}, table) - 2.5) < 1e-9


def test_unknown_model_message_is_short():
    big = {f"model-{i}": {"in": 1.0, "out": 2.0} for i in range(300)}
    try:
        g.cost("nope", {"input": 1}, big)
        raise AssertionError("should have exited")
    except SystemExit as e:
        assert len(str(e)) < 400, "the unknown-model error must not dump 300 model names"


def test_check_streams_jsonl_and_skips_unpriced():
    import contextlib
    import io
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "log.jsonl")
        with open(path, "w") as f:
            f.write(json.dumps({"model": "claude-opus-5-5", "input_tokens": 1_000_000,
                                "output_tokens": 0}) + "\n")
            f.write(json.dumps({"model": "nobody/nothing", "prompt_tokens": 10}) + "\n")
            f.write(json.dumps({"model": "claude-opus-5-5", "input_tokens": 1_000_000,
                                "output_tokens": 0}) + "\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            g.main(["check", path])
        out = buf.getvalue()
    assert "requests   3" in out, out
    assert "unpriced   1" in out, out
    assert "true cost  $8.00" in out, out  # 2 x 1M input at $4/M


def test_estimate_counts_tool_schemas():
    plain = g.estimate_tokens({"messages": [{"content": "hi"}]})
    with_tools = g.estimate_tokens({"messages": [{"content": "hi"}],
                                   "tools": [{"name": "x", "parameters": {"a": "b" * 400}}]})
    assert with_tools > plain + 50


def test_bad_content_length_is_rejected_not_a_crash():
    import http.client
    up = FakeUpstream()
    tr = g.Treasury(1.0)
    srv, url = serve(tr, up.url)
    try:
        host, port = url.replace("http://", "").split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=5)
        conn.putrequest("POST", "/v1/chat/completions")
        conn.putheader("content-length", "banana")
        conn.endheaders()
        assert conn.getresponse().status == 400
        conn.close()
    finally:
        srv.shutdown()
        up.stop()


def test_money_shows_small_numbers():
    assert g.money(0.0005) == "$0.0005" and g.money(5) == "$5.00"


# ---------------------------------------------------------------- the breaker

def test_breaker_refuses_before_spending():
    w = g.Wallet(1.00)
    w.charge(0.60)
    try:
        w.charge(0.60)
        raise AssertionError("breaker should have refused")
    except g.Tripped as t:
        assert t.would_be == 1.2
    assert w.spent == 0.60, "money must not move on a refused request"


def test_agent_cap_refuses_while_total_has_room():
    tr = g.Treasury(100, None, {"ci": {"limit": 0.05}})
    tr.charge("ci", 0.04)
    assert tr.blocker("ci", 0.02) and "agent 'ci'" in tr.blocker("ci", 0.02)
    assert tr.blocker("other", 0.02) is None, "other agents are unaffected"


def test_agent_spend_survives_restart():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "l.jsonl")
        g.Treasury(100, p, {"ci": {"limit": 1}}).charge("ci", 0.4, model="m")
        again = g.Treasury(100, p, {"ci": {"limit": 1}})
        assert again.agents["ci"]["spent"] == 0.4, "a restart must not reset a cap"


def test_daily_cap_resets_on_a_new_day():
    tr = g.Treasury(100, None, {"ci": {"limit": 1, "per": "day"}})
    tr.charge("ci", 0.9)
    assert tr.blocker("ci", 0.5)
    tr.today = "1970-01-01"  # pretend the date rolled over
    tr.charge("ci", 0.5)
    assert tr.agents["ci"]["spent"] == 0.5


def test_ledger_detects_edit():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "ledger.jsonl")
        w = g.Wallet(10, p)
        w.charge(0.1, turn=1)
        w.charge(0.2, turn=2)
        assert g.verify_ledger(p)[0] is True
        records = [json.loads(line) for line in open(p)]
        records[0]["cost"] = 0.0000001
        with open(p, "w") as f:
            f.write("\n".join(json.dumps(r, sort_keys=True) for r in records) + "\n")
        ok, _, msg = g.verify_ledger(p)
        assert not ok and "edited" in msg


def test_reopened_wallet_keeps_counting():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "ledger.jsonl")
        g.Wallet(10, p).charge(1.5)
        w2 = g.Wallet(10, p)
        assert w2.spent == 1.5
        try:
            w2.charge(9.0)
            raise AssertionError("reopened wallet lost its history")
        except g.Tripped:
            pass


def test_box_trips():
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        g.main(["box", "--limit", "5", "--model", "gpt-6-astra", "--context", "200000"])
    assert "BREAKER TRIPPED" in buf.getvalue()


def test_version():
    try:
        g.main(["--version"])
        raise AssertionError("should have exited")
    except SystemExit as e:
        assert e.code == 0


# ---------------------------------------------------------------- the proxy

def test_estimate_tokens():
    assert g.estimate_tokens({"messages": [{"content": "a" * 400}]}) == 100
    assert g.estimate_tokens({"messages": [{"content": [{"text": "a" * 40}]}]}) == 10


def test_proxy_refuses_before_forwarding():
    up = FakeUpstream()
    tr = g.Treasury(0.01)
    srv, url = serve(tr, up.url)
    try:
        try:
            post(url, {"model": "m", "max_tokens": 1000,
                       "messages": [{"content": "hi" * 2000}]})
            raise AssertionError("over-budget request was allowed through")
        except urllib.error.HTTPError as e:
            assert e.code == 402
            assert json.loads(e.read())["error"]["type"] == "governer_breaker_tripped"
        assert up.hits == 0, "refused request must never reach the provider"
        assert tr.spent == 0.0, "a refused request must not move money"
    finally:
        srv.shutdown()
        up.stop()


def test_proxy_bills_the_real_usage_block():
    up = FakeUpstream()
    tr = g.Treasury(1.0)
    srv, url = serve(tr, up.url)
    try:
        r = post(url, {"model": "m", "max_tokens": 50, "messages": [{"content": "hi"}]})
        assert r.status == 200 and r.headers["x-governer-spent"]
        assert abs(tr.spent - (2000 * 10 / 1e6 + 100 * 50 / 1e6)) < 1e-9, tr.spent
    finally:
        srv.shutdown()
        up.stop()


def test_proxy_attributes_spend_to_the_agent_header():
    up = FakeUpstream()
    tr = g.Treasury(1.0, None, {"research": {"limit": 1}})
    srv, url = serve(tr, up.url)
    try:
        post(url, {"model": "m", "max_tokens": 50, "messages": [{"content": "hi"}]},
             agent="research")
        assert tr.agents["research"]["spent"] > 0
        assert json.loads(urllib.request.urlopen(url + "/__governer").read())["agents"]["research"]["spent"] > 0
    finally:
        srv.shutdown()
        up.stop()


def test_dry_run_counts_but_never_refuses():
    up = FakeUpstream()
    tr = g.Treasury(0.0001)
    alerts = []
    srv, url = serve(tr, up.url, dry=True, notify_log=alerts)
    try:
        r = post(url, {"model": "m", "max_tokens": 50, "messages": [{"content": "hi"}]})
        assert r.status == 200 and up.hits == 1
        assert alerts == [], "dry run must not fire breach alerts"
    finally:
        srv.shutdown()
        up.stop()


def test_trip_fires_one_alert():
    up = FakeUpstream()
    tr = g.Treasury(0.01)
    alerts = []
    srv, url = serve(tr, up.url, notify_log=alerts)
    try:
        try:
            post(url, {"model": "m", "max_tokens": 1000, "messages": [{"content": "hi" * 2000}]})
        except urllib.error.HTTPError:
            pass
        assert len(alerts) == 1 and alerts[0]["event"] == "breaker_tripped"
    finally:
        srv.shutdown()
        up.stop()


def test_bad_body_is_400_not_a_crash():
    up = FakeUpstream()
    tr = g.Treasury(1.0)
    srv, url = serve(tr, up.url)
    try:
        for payload in (b"not json", b"[1,2,3]", b'"string"'):
            try:
                post(url, None, raw=payload)
                raise AssertionError(f"accepted bad body {payload!r}")
            except urllib.error.HTTPError as e:
                assert e.code == 400
        assert up.hits == 0
    finally:
        srv.shutdown()
        up.stop()


def test_streaming_is_refused_clearly():
    up = FakeUpstream()
    tr = g.Treasury(1.0)
    srv, url = serve(tr, up.url)
    try:
        try:
            post(url, {"model": "m", "stream": True, "messages": [{"content": "hi"}]})
            raise AssertionError("stream should be refused")
        except urllib.error.HTTPError as e:
            assert e.code == 400 and "stream" in json.loads(e.read())["error"]["message"]
        assert up.hits == 0
    finally:
        srv.shutdown()
        up.stop()


def test_embedded_prices_match_the_file():
    here = os.path.dirname(os.path.abspath(g.__file__))
    with open(os.path.join(here, "prices.json")) as f:
        curated = json.load(f)["models"]
    assert set(curated) == set(g._FALLBACK), "curated table and embedded fallback drifted apart"
    for name, row in g._FALLBACK.items():
        assert curated[name]["in"] == row["in"] and curated[name]["out"] == row["out"], name


def test_broken_webhook_does_not_break_the_breaker():
    g.notify("http://127.0.0.1:1/hook", {"event": "x"})  # must not raise
    g.notify("", {"event": "x"})


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except Exception as e:
                failed += 1
                print(f"  FAIL {name}: {type(e).__name__}: {e}")
    print("all good" if not failed else f"{failed} failed")
    raise SystemExit(1 if failed else 0)
