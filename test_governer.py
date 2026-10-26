"""Checks for governer. Run: python3 test_governer.py   (pytest also works)."""
import json
import os
import tempfile

import governer as g

P = {"m": {"in": 10.0, "out": 50.0}}  # gpt-6-astra shaped


def test_cost_basic():
    assert abs(g.cost("m", {"input": 1_000_000, "output": 1_000_000}, P) - 60.0) < 1e-9


def test_cache_read_is_a_tenth():
    full = g.cost("m", {"input": 1_000_000}, P)
    cached = g.cost("m", {"cache_read": 1_000_000}, P)
    assert abs(cached - full * 0.1) < 1e-9


def test_provider_field_spellings_normalize():
    a = g.normalize({"prompt_tokens": 5, "completion_tokens": 2, "cached_tokens": 1})
    b = g.normalize({"input_tokens": 5, "output_tokens": 2, "cache_read_input_tokens": 1})
    assert a == b == {"input": 5, "output": 2, "cache_read": 1}


def test_breaker_refuses_before_spending():
    w = g.Wallet(1.00)
    w.charge(0.60)
    try:
        w.charge(0.60)
        raise AssertionError("breaker should have refused")
    except g.Tripped as t:
        assert t.would_be == 1.2
    assert w.spent == 0.60, "money must not move on a refused request"


def test_ledger_detects_edit():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "ledger.jsonl")
        w = g.Wallet(10, p)
        w.charge(0.1, turn=1)
        w.charge(0.2, turn=2)
        assert g.verify_ledger(p)[0] is True
        records = [json.loads(line) for line in open(p)]
        records[0]["cost"] = 0.0000001  # someone shaves the receipt
        with open(p, "w") as f:
            f.write("\n".join(json.dumps(r, sort_keys=True) for r in records) + "\n")
        ok, n, msg = g.verify_ledger(p)
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
    out = buf.getvalue()
    assert "BREAKER TRIPPED" in out
    assert "paid" in out


def test_estimate_tokens():
    assert g.estimate_tokens({"messages": [{"content": "a" * 400}]}) == 100
    assert g.estimate_tokens({"messages": [{"content": [{"text": "a" * 40}]}]}) == 10


def test_proxy_refuses_before_forwarding():
    """The upstream here is a dead port: if it were contacted the test would 502."""
    import threading
    import urllib.error
    import urllib.request
    w = g.Wallet(0.01)
    cfg = {"upstream": "http://127.0.0.1:1", "rate": lambda m: (10 / 1e6, 50 / 1e6), "timeout": 5}
    srv = g.ThreadingHTTPServer(("127.0.0.1", 0), g.make_handler(w, cfg))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        body = json.dumps({"model": "x", "max_tokens": 1000,
                           "messages": [{"content": "hi" * 2000}]}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions",
                                     data=body, method="POST")
        req.add_header("content-type", "application/json")
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("over-budget request was allowed through")
        except urllib.error.HTTPError as e:
            assert e.code == 402
            assert json.loads(e.read())["error"]["type"] == "governer_breaker_tripped"
        assert w.spent == 0.0, "a refused request must not move money"
    finally:
        srv.shutdown()


def test_proxy_bills_the_real_usage_block():
    import threading
    import urllib.request
    from http.server import BaseHTTPRequestHandler

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length") or 0))
            out = json.dumps({"choices": [{"message": {"content": "ok"}}],
                              "usage": {"prompt_tokens": 2000, "completion_tokens": 100}}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    up = g.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    w = g.Wallet(1.0)
    cfg = {"upstream": f"http://127.0.0.1:{up.server_address[1]}",
           "rate": lambda m: (10 / 1e6, 50 / 1e6), "timeout": 5}
    srv = g.ThreadingHTTPServer(("127.0.0.1", 0), g.make_handler(w, cfg))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        body = json.dumps({"model": "x", "max_tokens": 50,
                           "messages": [{"content": "hi"}]}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions",
                                     data=body, method="POST")
        req.add_header("content-type", "application/json")
        r = urllib.request.urlopen(req, timeout=5)
        assert r.status == 200 and r.headers["x-governer-spent"]
        assert abs(w.spent - (2000 * 10 / 1e6 + 100 * 50 / 1e6)) < 1e-9, w.spent
    finally:
        up.shutdown()
        srv.shutdown()


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
