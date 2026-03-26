#!/usr/bin/env python3
"""Schematic-side launcher for AI PCB Assistant.

Use this from KiCad Schematic Editor via Tools -> External Tools.
It avoids the pcbnew-only ActionPlugin limitation and gives Eeschema a
lightweight entry point for generation and ngspice checks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, TOP, VERTICAL, X, Y, filedialog, messagebox, ttk
import tkinter as tk

BACKEND_URL = os.environ.get("AI_PCB_BACKEND_URL", "http://127.0.0.1:8765")
REQUEST_TIMEOUT = 120


class SchematicAssistantApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("AI PCB Assistant - Schematic Companion")
        self.root.geometry("760x640")
        self.root.configure(bg="#10141c")
        self._build_ui()
        self.refresh_health()

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill=BOTH, expand=True)

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Title.TLabel", font=("Helvetica", 18, "bold"), foreground="#edf2ff", background="#10141c")
        style.configure("Sub.TLabel", font=("Helvetica", 10), foreground="#94a3b8", background="#10141c")
        style.configure("Section.TLabel", font=("Helvetica", 9, "bold"), foreground="#7dd3fc", background="#10141c")

        ttk.Label(outer, text="AI PCB Assistant", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="Schematic-side companion for generation and simulation checks.",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(4, 14))

        ttk.Label(outer, text="Prompt", style="Section.TLabel").pack(anchor="w")
        self.prompt = tk.Text(outer, height=7, wrap="word", bg="#161c27", fg="#f8fafc", insertbackground="#f8fafc", relief="flat")
        self.prompt.pack(fill=X, pady=(6, 12))
        self.prompt.insert("1.0", "12V to 3.3V regulator for sensor board with status LED")

        btn_row = ttk.Frame(outer)
        btn_row.pack(fill=X, pady=(0, 12))
        ttk.Button(btn_row, text="Generate Schematic", command=self.generate).pack(side=LEFT)
        ttk.Button(btn_row, text="Check ngspice", command=self.check_ngspice).pack(side=LEFT, padx=(10, 0))
        ttk.Button(btn_row, text="Refresh Backend", command=self.refresh_health).pack(side=LEFT, padx=(10, 0))

        ttk.Label(outer, text="Status", style="Section.TLabel").pack(anchor="w")
        self.status_var = tk.StringVar(value="Checking backend...")
        ttk.Label(outer, textvariable=self.status_var, style="Sub.TLabel").pack(anchor="w", pady=(6, 10))

        ttk.Label(outer, text="Output", style="Section.TLabel").pack(anchor="w")
        output_wrap = ttk.Frame(outer)
        output_wrap.pack(fill=BOTH, expand=True, pady=(6, 0))
        self.output = tk.Text(output_wrap, wrap="word", bg="#0f172a", fg="#dbeafe", insertbackground="#dbeafe", relief="flat")
        self.output.pack(side=LEFT, fill=BOTH, expand=True)
        scroll = ttk.Scrollbar(output_wrap, orient=VERTICAL, command=self.output.yview)
        scroll.pack(side=RIGHT, fill=Y)
        self.output.configure(yscrollcommand=scroll.set)

    def _request_json(self, path: str, payload: dict | None = None) -> dict:
        req = urllib.request.Request(
            f"{BACKEND_URL}{path}",
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={"Content-Type": "application/json"},
            method="POST" if payload is not None else "GET",
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode())

    def set_output(self, text: str) -> None:
        self.output.delete("1.0", END)
        self.output.insert("1.0", text)

    def refresh_health(self) -> None:
        try:
            health = self._request_json("/health")
            mode = "healthy" if health.get("llm_loaded") else "running"
            self.status_var.set(f"Backend ready at {BACKEND_URL} ({mode})")
            caps = ", ".join(health.get("capabilities", [])[:6])
            self.set_output(f"Backend URL: {BACKEND_URL}\nStatus: {health.get('status')}\nCapabilities: {caps}")
        except Exception as exc:
            self.status_var.set(f"Backend unavailable: {exc}")
            self.set_output("Start the backend first, then refresh.\n\nExpected URL: " + BACKEND_URL)

    def generate(self) -> None:
        prompt = self.prompt.get("1.0", END).strip()
        if not prompt:
            messagebox.showwarning("Generate Schematic", "Enter a prompt first.")
            return
        self.status_var.set("Generating schematic...")
        try:
            result = self._request_json("/generate", {"prompt": prompt, "priority": "quality"})
            if not result.get("success"):
                raise RuntimeError(result.get("error", "Unknown backend error"))

            download_url = result.get("download_url")
            output_lines = [
                f"Mode: {result.get('generation_mode')}",
                f"Support: {result.get('support_status')}",
                f"Template: {result.get('template_used')}",
                f"Warnings: {result.get('warnings') or []}",
                "",
            ]

            if download_url:
                req = urllib.request.Request(f"{BACKEND_URL}{download_url}", method="GET")
                with urllib.request.urlopen(req, timeout=30) as resp:
                    sch_data = resp.read()
                default_name = download_url.split("/")[-1]
                save_path = filedialog.asksaveasfilename(
                    title="Save Generated Schematic",
                    defaultextension=".kicad_sch",
                    initialfile=default_name,
                    filetypes=[("KiCad Schematic", "*.kicad_sch")],
                )
                if save_path:
                    Path(save_path).write_bytes(sch_data)
                    output_lines.append(f"Saved schematic to: {save_path}")
                    output_lines.append("")
                    if sys.platform == "darwin":
                        subprocess.Popen(["open", save_path])
                else:
                    output_lines.append("Save cancelled.")
            else:
                output_lines.append("No downloadable schematic was returned.")

            self.status_var.set("Schematic generation complete.")
            self.set_output("\n".join(output_lines))
        except Exception as exc:
            self.status_var.set("Generation failed.")
            self.set_output(str(exc))
            messagebox.showerror("Generate Schematic", str(exc))

    def check_ngspice(self) -> None:
        ngspice_path = shutil.which("ngspice") if 'shutil' in globals() else None
        if ngspice_path is None:
            import shutil as _shutil
            ngspice_path = _shutil.which("ngspice")
        lines = ["ngspice integration status", "", f"Backend URL: {BACKEND_URL}", f"ngspice executable: {ngspice_path or 'Not installed'}"]
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
        self.status_var.set("Checked ngspice integration.")
        self.set_output("\n".join(lines))


def main() -> None:
    root = tk.Tk()
    app = SchematicAssistantApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
