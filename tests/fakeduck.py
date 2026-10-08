"""A stand-in for duck.ai: the same buttons and the same /duckchat/v1/chat SSE stream,
with switches for the ways the real one fails (429, silence, a stuck model picker)."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELS = [("gpt-5-mini", "GPT-5 mini"), ("gpt-4o-mini", "GPT-4o mini"),
          ("tinfoil/gpt-oss-120b", "gpt-oss 120B"), ("claude-haiku-4-5", "Claude Haiku 4.5"),
          ("meta-llama/Llama-4-Scout-17B-16E-Instruct", "Llama 4 Scout"),
          ("mistral-small-2603", "Mistral Small 4")]

STATE = {"mode": "ok", "requests": [], "page": "normal", "aborted": 0}

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Duck.ai</title></head><body>
<button id="model">GPT-5 mini</button>
<button id="newchat">New Chat</button>
<div id="dialog" role="dialog" hidden>
CARDS
<button id="confirm">Start New Chat</button>
</div>
<div id="log"></div>
<textarea name="user-prompt"></textarea>
SENDBTN
<button id="stop" hidden>Stop generating</button>
<script>
const STUCK = STUCKFLAG;
let model = 'gpt-5-mini', pending = model, thread = [], ctl = null;
const $ = id => document.getElementById(id);
const ta = document.querySelector("textarea[name='user-prompt']");
$('model').onclick = () => { $('dialog').hidden = false; };
document.querySelectorAll('.card').forEach(c => c.onclick = () => { pending = c.dataset.id; });
$('confirm').onclick = () => {
  if (!STUCK) model = pending;
  $('dialog').hidden = true; thread = [];
  $('model').textContent = document.querySelector(`.card[data-id="${model}"]`).textContent;
  if ($('send')) $('send').textContent = 'Ask';
};
$('newchat').onclick = () => {
  thread = []; $('log').textContent = '';
  if ($('send')) $('send').textContent = 'Ask';
};
$('stop').onclick = () => { if (ctl) ctl.abort(); };
if ($('send')) $('send').onclick = () => {
  const text = ta.value; if (!text) return;
  ta.value = ''; ta.disabled = true; $('stop').hidden = false; $('send').textContent = 'Send';
  ctl = new AbortController();
  thread.push({role: 'user', content: text});
  fetch('/duckchat/v1/chat', {method: 'POST', headers: {'content-type': 'application/json'},
                              body: JSON.stringify({model, messages: thread}), signal: ctl.signal})
    .then(async r => {
      const rd = r.body.getReader();
      for (;;) { const {done} = await rd.read(); if (done) break; }
    })
    .catch(() => {})
    .finally(() => { ta.disabled = false; $('stop').hidden = true; });
};
</script></body></html>"""


def page_html():
    cards = "\n".join(f'<div class="card" data-id="{i}">{label}</div>' for i, label in MODELS)
    send = "" if STATE["page"] == "nosend" else '<button id="send">Ask</button>'
    stuck = "true" if STATE["page"] == "stuck" else "false"
    return (PAGE.replace("CARDS", cards).replace("SENDBTN", send)
            .replace("STUCKFLAG", stuck))


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = page_html().encode()
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        STATE["requests"].append(req)
        mode = STATE["mode"]
        if mode in ("429", "429once"):
            if mode == "429once":
                STATE["mode"] = "ok"
            body = b'{"action":"error","status":429,"type":"ERR_BN_LIMIT"}'
            self.send_response(429)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        last = req["messages"][-1]["content"]
        if mode == "hang":
            time.sleep(8)
            return
        if mode == "slow":
            parts = [f"word{i} " for i in range(200)]
            delay = 0.1
        else:
            reply = f"echo[{req['model']}] {last[:60]}\n\n```python\nprint('hi')\n```\n"
            parts = [reply[i:i + 6] for i in range(0, len(reply), 6)]
            delay = 0.01
        try:
            for p in parts:
                self.wfile.write(f"data: {json.dumps({'message': p, 'role': 'assistant'})}\n\n"
                                 .encode())
                self.wfile.flush()
                time.sleep(delay)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            STATE["aborted"] += 1


def serve() -> str:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}/"
