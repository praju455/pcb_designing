#!/usr/bin/env python3
"""Dependency-free schematic-side launcher for AI PCB Assistant.

Starts a tiny local web UI so it can be used even when tkinter is unavailable.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

BACKEND_URL = os.environ.get("AI_PCB_BACKEND_URL", "http://127.0.0.1:8765")
REQUEST_TIMEOUT = 120
DEFAULT_PROMPT = "12V to 3.3V regulator for sensor board with status LED"

HTML = """<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
  <title>AI PCB Assistant - Schematic Companion</title>
  <style>
    :root {
      --bg: #0f172a;
      --panel: #111827;
      --panel-2: #1f2937;
      --border: #334155;
      --text: #e5eefc;
      --muted: #94a3b8;
      --accent: #38bdf8;
      --ok: #22c55e;
      --warn: #f59e0b;
      --danger: #ef4444;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      padding: 24px;
      background: linear-gradient(180deg, #0b1220, #0f172a 35%, #111827);
      color: var(--text);
      font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .wrap { max-width: 920px; margin: 0 auto; }
    .hero, .panel {
      background: rgba(17, 24, 39, 0.92);
      border: 1px solid var(--border);
      border-radius: 18px;
      box-shadow: 0 18px 48px rgba(0,0,0,0.28);
    }
    .hero { padding: 24px; margin-bottom: 18px; }
    .eyebrow {
      color: var(--accent);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.14em;
      text-transform: uppercase;
    }
    h1 { margin: 10px 0 10px; font-size: 34px; line-height: 1.1; }
    .sub { color: var(--muted); max-width: 720px; }
    .statusbar {
      display: inline-block;
      margin-top: 14px;
      padding: 8px 12px;
      border-radius: 999px;
      background: #13233a;
      color: #bae6fd;
      font-size: 13px;
    }
    .panel { padding: 20px; margin-bottom: 18px; }
    label { display: block; margin-bottom: 8px; color: #cbd5e1; font-weight: 600; }
    textarea {
      width: 100%;
      min-height: 150px;
      resize: vertical;
      background: #0b1220;
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 14px;
      font: 15px/1.5 Menlo, Monaco, monospace;
    }
    .row { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 14px; }
    button {
      border: 0;
      border-radius: 12px;
      padding: 11px 16px;
      font: 600 14px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      cursor: pointer;
      color: white;
    }
    button.primary { background: #2563eb; }
    button.secondary { background: #475569; }
    button.ghost { background: #1f2937; border: 1px solid var(--border); }
    pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      background: #08101d;
      color: #dbeafe;
      border: 1px solid #223247;
      border-radius: 14px;
      padding: 16px;
      min-height: 220px;
      font: 13px/1.5 Menlo, Monaco, monospace;
    }
    .muted { color: var(--muted); }
  </style>
</head>
<body>
  <div class=\"wrap\">
    <section class=\"hero\">
      <div class=\"eyebrow\">AI PCB Assistant</div>
      <h1>Schematic Companion</h1>
      <div class=\"sub\">Launch AI schematic generation and simulation checks from the schematic side without depending on the PCB-only KiCad plugin API.</div>
      <div class=\"statusbar\" id=\"statusline\">Starting local companion...</div>
    </section>

    <section class=\"panel\">
      <label for=\"prompt\">Prompt</label>
      <textarea id=\"prompt\"></textarea>
      <div class=\"row\">
        <button class=\"primary\" onclick=\"generateSchematic()\">Generate Schematic</button>
        <button class=\"secondary\" onclick=\"checkNgspice()\">Check ngspice</button>
        <button class=\"ghost\" onclick=\"refreshHealth()\">Refresh Backend</button>
      </div>
    </section>

    <section class=\"panel\">
      <label>Output</label>
      <pre id=\"output\">Loading...</pre>
      <div class=\"muted\" style=\"margin-top:10px;\">Downloads use your browser's normal save flow.</div>
    </section>
  </div>

  <script>
    const DEFAULT_PROMPT = %DEFAULT_PROMPT%;
    document.getElementById('prompt').value = DEFAULT_PROMPT;

    function setOutput(text) {
      document.getElementById('output').textContent = text;
    }

    function setStatus(text) {
      document.getElementById('statusline').textContent = text;
    }

    async function refreshHealth() {
      setStatus('Checking backend...');
      try {
        const resp = await fetch('/api/health');
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || 'Backend unavailable');
        setStatus(`Backend ready at ${data.backend_url} (${data.mode})`);
        setOutput(`Backend URL: ${data.backend_url}\nStatus: ${data.status}\nCapabilities: ${data.capabilities.join(', ') || 'none'}`);
      } catch (err) {
        setStatus('Backend unavailable');
        setOutput(String(err));
      }
    }

    async function checkNgspice() {
      setStatus('Checking ngspice...');
      try {
        const resp = await fetch('/api/ngspice');
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || 'Check failed');
        setStatus('Checked ngspice integration.');
        setOutput(data.text);
      } catch (err) {
        setStatus('ngspice check failed');
        setOutput(String(err));
      }
    }

    async function generateSchematic() {
      const prompt = document.getElementById('prompt').value.trim();
      if (!prompt) {
        setOutput('Enter a prompt first.');
        return;
      }
      setStatus('Generating schematic...');
      try {
        const resp = await fetch('/api/generate', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({prompt})
        });
        const data = await resp.json();
        if (!resp.ok || !data.success) throw new Error(data.error || 'Generation failed');
        const lines = [
          `Mode: ${data.generation_mode}`,
          `Support: ${data.support_status}`,
          `Template: ${data.template_used}`,
          `Warnings: ${JSON.stringify(data.warnings || [])}`,
          ''
        ];
        if (data.download_url) {
          const url = `/download${data.download_url}`;
          lines.push(`Download ready: ${url}`);
          window.open(url, '_blank');
        } else {
          lines.push('No downloadable schematic was returned.');
        }
        setStatus('Schematic generation complete.');
        setOutput(lines.join('\n'));
      } catch (err) {
        setStatus('Generation failed');
        setOutput(String(err));
      }
    }

    refreshHealth();
  </script>
</body>
</html>
"""


def _request_json(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    req = urllib.request.Request(
        f"{BACKEND_URL}{path}",
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


class CompanionHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(HTML.replace("%DEFAULT_PROMPT%", json.dumps(DEFAULT_PROMPT)))
            return
        if parsed.path == "/api/health":
            try:
                health = _request_json("/health")
                payload = {
                    "backend_url": BACKEND_URL,
                    "status": health.get("status"),
                    "mode": "healthy" if health.get("llm_loaded") else "running",
                    "capabilities": health.get("capabilities", []),
                }
                self._send_json(payload)
            except Exception as exc:
                self._send_json({"error": str(exc)}, 500)
            return
        if parsed.path == "/api/ngspice":
            ngspice_path = shutil.which("ngspice")
            lines = [
                "ngspice integration status",
                "",
                f"Backend URL: {BACKEND_URL}",
                f"ngspice executable: {ngspice_path or 'Not installed'}",
            ]
            if not ngspice_path:
                lines += ["", "Install ngspice first, for example on macOS:", "brew install ngspice"]
            else:
                try:
                    proc = subprocess.run([ngspice_path, "-v"], capture_output=True, text=True, timeout=10)
                    version_line = (proc.stdout or proc.stderr or "").strip().splitlines()
                    if version_line:
                        lines += ["", f"Detected: {version_line[0]}"]
                except Exception as exc:
                    lines += ["", f"Failed to query ngspice version: {exc}"]
                lines += ["", "Open KiCad Schematic Editor -> Inspect -> Simulator for waveform simulation."]
            self._send_json({"text": "\n".join(lines)})
            return
        if parsed.path.startswith("/download/"):
            target = f"{BACKEND_URL}{parsed.path.replace('/download', '')}"
            try:
                req = urllib.request.Request(target, method="GET")
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = resp.read()
                    content_type = resp.headers.get("Content-Type", "application/octet-stream")
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Disposition", f"attachment; filename={Path(parsed.path).name}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                self._send_json({"error": str(exc)}, 500)
            return
        self._send_json({"error": "Not found"}, 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/generate":
            self._send_json({"error": "Not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b"{}"
        payload = json.loads(body.decode("utf-8"))
        prompt = (payload.get("prompt") or "").strip()
        if not prompt:
            self._send_json({"error": "Prompt is required."}, 400)
            return
        try:
            result = _request_json("/generate", {"prompt": prompt, "priority": "quality"})
            self._send_json(result)
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> None:
    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), CompanionHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    webbrowser.open(f"http://127.0.0.1:{port}/")
    print(f"AI PCB Assistant schematic companion running at http://127.0.0.1:{port}/")
    try:
        thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
