"""
AI PCB Assistant - Advanced KiCad Action Plugin v2.0
Professional-grade PCB design automation with real-time AI assistance.
"""

from __future__ import annotations

import json
import logging
import math
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
from urllib.parse import urljoin

import pcbnew
import wx

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

@dataclass
class PluginConfig:
    """Plugin configuration with persistence."""
    backend_url: str = "http://localhost:8765"
    request_timeout: int = 120
    auto_refresh: bool = True
    show_ratsnest: bool = True
    placement_step_size: float = 1.0  # mm
    thermal_aware: bool = True
    constraint_driven: bool = True
    freerouting_jar: str = ""
    freerouting_timeout: int = 900
    
    # File paths
    config_dir: str = field(default_factory=lambda: os.path.join(
        os.path.expanduser("~"), ".ai_pcb_assistant"
    ))
    
    def __post_init__(self):
        os.makedirs(self.config_dir, exist_ok=True)
    
    def save(self):
        """Save configuration to disk."""
        path = os.path.join(self.config_dir, "config.json")
        with open(path, 'w') as f:
            json.dump(self.__dict__, f, indent=2)
    
    @classmethod
    def load(cls) -> PluginConfig:
        """Load configuration from disk."""
        path = os.path.join(os.path.expanduser("~"), ".ai_pcb_assistant", "config.json")
        if os.path.exists(path):
            with open(path, 'r') as f:
                data = json.load(f)
                return cls(**data)
        return cls()

CONFIG = PluginConfig.load()


# ── Data Structures ───────────────────────────────────────────────────────────

class NetType(Enum):
    """Classification of nets for optimization."""
    SIGNAL = auto()
    POWER = auto()
    GROUND = auto()
    CLOCK = auto()
    DIFFERENTIAL = auto()
    HIGH_SPEED = auto()
    ANALOG = auto()


@dataclass
class NetInfo:
    """Enhanced net information."""
    name: str
    code: int
    net_type: NetType
    pins: List[Dict[str, str]] = field(default_factory=list)
    length_mm: float = 0.0
    is_critical: bool = False
    matched_length: Optional[str] = None  # For differential pairs
    
    @property
    def component_count(self) -> int:
        return len(set(p["ref"] for p in self.pins))


@dataclass
class ComponentInfo:
    """Enhanced component information."""
    ref: str
    value: str
    footprint: str
    x: float
    y: float
    rotation: float
    layer: str
    width: float = 0.0
    height: float = 0.0
    pins: List[Dict] = field(default_factory=list)
    power_dissipation: float = 0.0
    is_fixed: bool = False
    cluster_id: Optional[str] = None


@dataclass
class Constraint:
    """Placement constraint."""
    type: str  # "fixed", "region", "alignment", "spacing"
    refs: List[str]
    params: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True


# ── Async HTTP Client ─────────────────────────────────────────────────────────

class AsyncHTTPClient:
    """Non-blocking HTTP client for backend communication."""
    
    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._results: Dict[str, Any] = {}
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
    
    def _worker(self):
        """Background worker thread."""
        while True:
            task = self._queue.get()
            if task is None:
                break
            
            request_id, url, data, callback = task
            try:
                req = urllib.request.Request(
                    url,
                    data=json.dumps(data).encode('utf-8') if data else None,
                    headers={"Content-Type": "application/json"},
                    method="POST" if data else "GET",
                )
                
                with urllib.request.urlopen(req, timeout=CONFIG.request_timeout) as resp:
                    result = json.loads(resp.read().decode())
                    self._results[request_id] = ("success", result)

            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    body = ""

                if body:
                    try:
                        parsed = json.loads(body)
                        detail = parsed.get("detail", parsed)
                        self._results[request_id] = ("error", f"HTTP {e.code}: {detail}")
                    except Exception:
                        self._results[request_id] = ("error", f"HTTP {e.code}: {body}")
                else:
                    self._results[request_id] = ("error", f"HTTP {e.code}: {e.reason}")

            except Exception as e:
                self._results[request_id] = ("error", str(e))
            
            if callback:
                wx.CallAfter(callback, request_id)
    
    def request(self, url: str, data: Optional[Dict] = None, 
                callback: Optional[Callable] = None) -> str:
        """Queue a request and return request ID."""
        request_id = f"{time.time():.6f}"
        self._queue.put((request_id, url, data, callback))
        return request_id
    
    def get_result(self, request_id: str) -> Tuple[str, Any]:
        """Get result for a request."""
        return self._results.pop(request_id, ("pending", None))
    
    def close(self):
        """Shutdown the worker thread."""
        self._queue.put(None)

# Global client
HTTP_CLIENT = AsyncHTTPClient()


# ── Visualization Canvas ──────────────────────────────────────────────────────

class PlacementPreviewCanvas(wx.ScrolledWindow):
    """Interactive canvas for placement preview.
    Falls back to a wx.ScrolledWindow with manual DC drawing when
    wx.lib.floatcanvas is not available in the KiCad Python environment.
    """

    def __init__(self, parent):
        super().__init__(parent, size=(400, 400),
                         style=wx.HSCROLL | wx.VSCROLL)
        self.SetScrollRate(5, 5)
        self.SetBackgroundColour(wx.Colour(40, 40, 40))
        self.Bind(wx.EVT_PAINT, self._on_paint)

        self.board_width = 100.0
        self.board_height = 80.0
        self.scale = 4.0  # pixels per mm
        self.components: Dict[str, Any] = {}
        self.nets: List[NetInfo] = []
        self.selected_refs: Set[str] = set()
        self.show_ratsnest = True
        self.Bind(wx.EVT_SIZE, self.on_size)

    # ── FloatCanvas path ──────────────────────────────────────────────────────

    def set_board_dimensions(self, width: float, height: float):
        self.board_width = width
        self.board_height = height
        self.Refresh()

    def update_components(self, components: List[ComponentInfo],
                          nets: Optional[List[NetInfo]] = None):
        self.components = {c.ref: c for c in components}
        if nets:
            self.nets = nets
        self._draw()

    def highlight_component(self, ref: str, color: str = "red"):
        if ref in self.components:
            self.selected_refs.add(ref)
            self._draw()

    def _draw(self):
        self.Refresh()



    # ── wx.ScrolledWindow (fallback) rendering ────────────────────────────────

    def _on_paint(self, event):
        """Fallback DC-based drawing when FloatCanvas is absent."""
        dc = wx.PaintDC(self)
        self.DoPrepareDC(dc)
        w, h = self.GetClientSize()
        dc.SetBackground(wx.Brush(wx.Colour(30, 30, 30)))
        dc.Clear()

        if w <= 0 or h <= 0:
            return

        # Board outline
        margin = 20
        board_px_w = w - 2 * margin
        board_px_h = h - 2 * margin
        dc.SetPen(wx.Pen(wx.Colour(0, 180, 0), 2))
        dc.SetBrush(wx.Brush(wx.Colour(20, 60, 20)))
        dc.DrawRectangle(margin, margin, board_px_w, board_px_h)

        if not self.components:
            dc.SetTextForeground(wx.Colour(120, 120, 120))
            dc.DrawText("No components loaded", margin + 10, margin + 10)
            return

        bw = self.board_width or 100.0
        bh = self.board_height or 80.0

        def to_px(x_mm, y_mm):
            px = int(margin + (x_mm / bw) * board_px_w)
            py = int(margin + (y_mm / bh) * board_px_h)
            return px, py

        # Ratsnest
        if self.show_ratsnest:
            dc.SetPen(wx.Pen(wx.Colour(80, 80, 80), 1, wx.PENSTYLE_DOT))
            for net in self.nets:
                positions = []
                for pin in net.pins:
                    if pin["ref"] in self.components:
                        c = self.components[pin["ref"]]
                        positions.append(to_px(c.x, c.y))
                for i in range(len(positions) - 1):
                    dc.DrawLine(*positions[i], *positions[i + 1])

        # Components
        for ref, comp in self.components.items():
            px, py = to_px(comp.x, comp.y)
            cw = max(6, int((comp.width / bw) * board_px_w * 0.8))
            ch = max(6, int((comp.height / bh) * board_px_h * 0.8))
            color = self._get_component_color_rgb(comp)
            if ref in self.selected_refs:
                color = wx.Colour(220, 50, 50)
            dc.SetPen(wx.Pen(wx.BLACK, 1))
            dc.SetBrush(wx.Brush(color))
            dc.DrawRectangle(px - cw // 2, py - ch // 2, cw, ch)
            dc.SetTextForeground(wx.WHITE)
            dc.SetFont(wx.Font(6, wx.FONTFAMILY_DEFAULT,
                               wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
            dc.DrawText(ref, px - cw // 2, py - ch // 2)

    # ── Helpers (shared) ──────────────────────────────────────────────────────

    def _get_component_color(self, comp: ComponentInfo) -> str:
        if comp.ref.startswith('U'):
            return "blue"
        elif comp.ref.startswith('R'):
            return "lightblue"
        elif comp.ref.startswith('C'):
            return "yellow"
        elif comp.ref.startswith('L'):
            return "orange"
        elif comp.ref[:1] in 'JP':
            return "green"
        return "gray"

    def _get_component_color_rgb(self, comp: ComponentInfo) -> wx.Colour:
        mapping = {
            'U': wx.Colour(60, 60, 200),
            'R': wx.Colour(100, 160, 220),
            'C': wx.Colour(200, 200, 60),
            'L': wx.Colour(220, 140, 40),
            'J': wx.Colour(60, 160, 80),
            'P': wx.Colour(60, 160, 80),
        }
        return mapping.get(comp.ref[:1], wx.Colour(120, 120, 120))

    def _get_net_color(self, net: NetInfo) -> str:
        if net.net_type == NetType.POWER:
            return "red"
        elif net.net_type == NetType.GROUND:
            return "green"
        elif net.net_type == NetType.CLOCK:
            return "purple"
        elif net.net_type == NetType.DIFFERENTIAL:
            return "orange"
        return "lightgray"

    def on_size(self, event):
        self.Refresh()
        event.Skip()


# ── Main Plugin Class ─────────────────────────────────────────────────────────

class AIPlacementPlugin(pcbnew.ActionPlugin):
    """Advanced KiCad plugin for AI-powered PCB design."""
    
    def defaults(self):
        self.name = "AI KiCad Plugin"
        self.category = "AI Tools"
        self.description = "AI-powered PCB generation, placement, and DFM dashboard"
        self.show_toolbar_button = True
        icon_path = os.path.join(os.path.dirname(__file__), "icon_32x32.png")
        self.icon_file_name = icon_path if os.path.exists(icon_path) else ""
    
    def Run(self):
        """Open the dashboard launcher for the current board."""
        try:
            board = pcbnew.GetBoard()
            if board is None:
                wx.MessageBox("No board is open.", "Error", wx.OK | wx.ICON_ERROR)
                return

            parent = self._get_top_window()

            if not self._check_backend():
                dlg = BackendSetupDialog(parent)
                if dlg.ShowModal() != wx.ID_OK:
                    dlg.Destroy()
                    return
                dlg.Destroy()

            # Initialize health check and UI safely
            dlg = AIDashboardDialog(parent, self, board)
            dlg.Show()
        except Exception as e:
            import traceback
            logger.error(traceback.format_exc())
            wx.MessageBox(f"Plugin Error: {e}", "AI PCB Assistant Error")

    def _ensure_frame(self, board: pcbnew.BOARD) -> "AIPCBFrame":
        # Check if frame exists and is still valid (not deleted)
        existing = getattr(self, "_frame", None)
        if existing:
            try:
                # wx objects can become 'dead' even if the Python object exists
                if not existing:
                    existing = None
                else:
                    existing.board = board
                    existing._extract_board_data()
                    return existing
            except Exception:
                existing = None
        
        self._frame = AIPCBFrame(self._get_top_window(), board)
        return self._frame

    def _get_top_window(self) -> Optional[wx.Window]:
        try:
            app = wx.GetApp()
            if hasattr(app, "GetTopWindow"):
                return app.GetTopWindow()
        except Exception:
            pass
        return None

    def _show_frame(self, board: pcbnew.BOARD) -> "AIPCBFrame":
        frame = self._ensure_frame(board)
        frame.Show()
        frame.Raise()
        return frame
    
    def _check_backend(self) -> bool:
        """Check if backend is available."""
        try:
            req = urllib.request.Request(
                f"{CONFIG.backend_url}/health", 
                method="GET"
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
                return data.get("status") in ("ok", "healthy", "degraded")
        except Exception as e:
            logger.warning(f"Backend check failed: {e}")
            return False


# ── Backend Setup Dialog ──────────────────────────────────────────────────────

class BackendSetupDialog(wx.Dialog):
    """Dialog for backend installation/configuration."""
    
    def __init__(self, parent):
        super().__init__(parent, title="AI Backend Setup", size=(500, 400))
        self._init_ui()
    
    def _init_ui(self):
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        
        # Header
        header = wx.StaticText(panel, label="AI Backend Not Detected")
        header.SetFont(wx.Font(14, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        sizer.Add(header, 0, wx.ALL | wx.ALIGN_CENTER, 10)
        
        # Options
        sizer.Add(wx.StaticText(panel, label="Choose an option:"), 0, wx.LEFT | wx.TOP, 10)
        
        self.rb_local = wx.RadioButton(panel, label="Start local backend (recommended)", style=wx.RB_GROUP)
        sizer.Add(self.rb_local, 0, wx.ALL, 5)
        
        self.rb_remote = wx.RadioButton(panel, label="Connect to remote backend")
        sizer.Add(self.rb_remote, 0, wx.ALL, 5)
        
        self.txt_url = wx.TextCtrl(panel, value=CONFIG.backend_url)
        sizer.Add(self.txt_url, 0, wx.EXPAND | wx.ALL, 5)
        
        # Auto-install option
        self.chk_install = wx.CheckBox(panel, label="Download and install backend automatically")
        self.chk_install.SetValue(True)
        sizer.Add(self.chk_install, 0, wx.ALL, 10)
        
        # Buttons
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_ok = wx.Button(panel, wx.ID_OK, label="Setup")
        btn_cancel = wx.Button(panel, wx.ID_CANCEL)
        btn_sizer.Add(btn_ok, 0, wx.ALL, 5)
        btn_sizer.Add(btn_cancel, 0, wx.ALL, 5)
        sizer.Add(btn_sizer, 0, wx.ALIGN_CENTER | wx.ALL, 10)
        
        panel.SetSizer(sizer)
        
        # Bind events
        btn_ok.Bind(wx.EVT_BUTTON, self._on_setup)
    
    def _on_setup(self, event):
        backend_url = self.txt_url.GetValue().strip() or CONFIG.backend_url
        CONFIG.backend_url = backend_url
        CONFIG.save()

        if self.chk_install.GetValue() and self.rb_local.GetValue():
            self._install_backend()

        if self._check_backend():
            self.EndModal(wx.ID_OK)
            return

        wx.MessageBox(
            "Backend is still not reachable. Start it and try again.\n"
            f"Expected URL: {CONFIG.backend_url}",
            "Backend Not Reachable",
            wx.OK | wx.ICON_WARNING,
        )
        event.Skip(False)
    
    def _install_backend(self):
        """Trigger backend installation."""
        candidate_paths = [
            os.path.join(os.path.dirname(__file__), "..", "ai_backend", "start_backend.bat"),
            os.path.join(os.getcwd(), "ai_backend", "start_backend.bat"),
        ]

        script = next((os.path.abspath(p) for p in candidate_paths if os.path.exists(p)), None)
        if not script:
            wx.MessageBox(
                "Could not find start_backend.bat automatically.\n\n"
                "Start backend manually:\n"
                "1) Open terminal in project root\n"
                "2) cd ai_backend\n"
                "3) start_backend.bat",
                "Backend Setup",
                wx.OK | wx.ICON_INFORMATION,
            )
            return

        try:
            subprocess.Popen(["cmd", "/c", script], cwd=os.path.dirname(script))
            wx.MessageBox(
                f"Starting backend:\n{script}\n\n"
                "Wait a few seconds, then click Setup again.",
                "Backend Setup",
                wx.OK | wx.ICON_INFORMATION,
            )
        except Exception as exc:
            wx.MessageBox(f"Failed to start backend:\n{exc}", "Backend Setup Error", wx.OK | wx.ICON_ERROR)

    def _check_backend(self) -> bool:
        try:
            req = urllib.request.Request(f"{CONFIG.backend_url}/health", method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
                return data.get("status") in ("ok", "healthy", "degraded")
        except Exception:
            return False


# ── Main Application Frame ────────────────────────────────────────────────────

class AIPCBFrame(wx.Frame):
    """Main application window with advanced features."""
    
    def __init__(self, parent, board: pcbnew.BOARD):
        super().__init__(parent, title="AI PCB Assistant Pro", size=(1200, 800))
        self.board = board
        self.components: List[ComponentInfo] = []
        self.nets: List[NetInfo] = []
        self.constraints: List[Constraint] = []
        self.pending_requests: Set[str] = set()
        self.request_types: Dict[str, str] = {}
        
        self._init_ui()
        self._extract_board_data()
        self._start_refresh_timer()
        self.Bind(wx.EVT_CLOSE, self._on_close)
    
    def _init_ui(self):
        """Initialize the main UI."""
        self.CreateStatusBar()
        self.SetStatusText("Ready")
        
        # Menu bar
        menubar = wx.MenuBar()
        file_menu = wx.Menu()
        file_menu.Append(wx.ID_EXIT, "E&xit\tCtrl+Q")
        self.Bind(wx.EVT_MENU, self._on_exit, id=wx.ID_EXIT)
        menubar.Append(file_menu, "&File")
        
        tools_menu = wx.Menu()
        tools_menu.Append(1001, "&Settings\tCtrl+,")
        tools_menu.Append(1002, "&Refresh Board Data\tF5")
        self.Bind(wx.EVT_MENU, self._on_settings, id=1001)
        self.Bind(wx.EVT_MENU, self._on_refresh, id=1002)
        menubar.Append(tools_menu, "&Tools")
        
        self.SetMenuBar(menubar)
        
        # Main splitter
        splitter = wx.SplitterWindow(self)
        
        # Left panel: Controls
        left_panel = self._create_left_panel(splitter)
        
        # Right panel: Visualization
        right_panel = self._create_right_panel(splitter)
        
        splitter.SplitVertically(left_panel, right_panel, 400)
        splitter.SetMinimumPaneSize(300)
        
        # Toolbar
        toolbar = self.CreateToolBar(wx.TB_HORIZONTAL | wx.TB_TEXT)
        toolbar.AddTool(101, "Optimize", wx.ArtProvider.GetBitmap(wx.ART_EXECUTABLE_FILE), "Optimize Placement")
        toolbar.AddTool(102, "DFM Check", wx.ArtProvider.GetBitmap(wx.ART_TICK_MARK), "Check DFM")
        toolbar.AddTool(103, "Generate", wx.ArtProvider.GetBitmap(wx.ART_NEW), "Generate Circuit")
        toolbar.Realize()
        
        self.Bind(wx.EVT_TOOL, self._on_optimize_tool, id=101)
        self.Bind(wx.EVT_TOOL, self._on_dfm_tool, id=102)
        self.Bind(wx.EVT_TOOL, self._on_generate_tool, id=103)
    
    def _create_left_panel(self, parent) -> wx.Panel:
        """Create left control panel."""
        panel = wx.ScrolledWindow(parent, size=(400, -1))
        sizer = wx.BoxSizer(wx.VERTICAL)
        
        # AI Assistant section
        box = wx.StaticBox(panel, label="AI Assistant")
        box_sizer = wx.StaticBoxSizer(box, wx.VERTICAL)
        
        self.prompt_ctrl = wx.TextCtrl(panel, style=wx.TE_MULTILINE, size=(-1, 100))
        self.prompt_ctrl.SetHint("Describe what you want to do...\n"
                                "Examples:\n"
                                "- 'Optimize for minimal EMI'\n"
                                "- 'Place decoupling caps near ICs'\n"
                                "- 'Group analog components together'")
        box_sizer.Add(self.prompt_ctrl, 0, wx.EXPAND | wx.ALL, 5)
        
        btn_run = wx.Button(panel, label="Execute")
        btn_run.Bind(wx.EVT_BUTTON, self._on_execute_prompt)
        box_sizer.Add(btn_run, 0, wx.ALIGN_RIGHT | wx.ALL, 5)
        
        sizer.Add(box_sizer, 0, wx.EXPAND | wx.ALL, 10)
        
        # Component list
        box2 = wx.StaticBox(panel, label="Components")
        box2_sizer = wx.StaticBoxSizer(box2, wx.VERTICAL)
        
        self.comp_list = wx.ListCtrl(panel, style=wx.LC_REPORT)
        self.comp_list.AppendColumn("Ref", width=60)
        self.comp_list.AppendColumn("Value", width=100)
        self.comp_list.AppendColumn("Footprint", width=120)
        self.comp_list.AppendColumn("X", width=50)
        self.comp_list.AppendColumn("Y", width=50)
        self.comp_list.AppendColumn("Fixed", width=50)
        self.comp_list.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_component_selected)
        box2_sizer.Add(self.comp_list, 1, wx.EXPAND | wx.ALL, 5)
        
        btn_fix = wx.Button(panel, label="Toggle Fixed")
        btn_fix.Bind(wx.EVT_BUTTON, self._on_toggle_fixed)
        box2_sizer.Add(btn_fix, 0, wx.ALL, 5)
        
        sizer.Add(box2_sizer, 1, wx.EXPAND | wx.ALL, 10)
        
        # Constraints
        box3 = wx.StaticBox(panel, label="Constraints")
        box3_sizer = wx.StaticBoxSizer(box3, wx.VERTICAL)
        
        self.constraint_list = wx.ListBox(panel)
        box3_sizer.Add(self.constraint_list, 1, wx.EXPAND | wx.ALL, 5)
        
        btn_add_constraint = wx.Button(panel, label="Add Constraint")
        btn_add_constraint.Bind(wx.EVT_BUTTON, self._on_add_constraint)
        box3_sizer.Add(btn_add_constraint, 0, wx.ALL, 5)
        
        sizer.Add(box3_sizer, 0, wx.EXPAND | wx.ALL, 10)
        
        panel.SetSizer(sizer)
        if hasattr(panel, "SetupScrolling"):
            panel.SetupScrolling()
        return panel
    
    def _create_right_panel(self, parent) -> wx.Panel:
        """Create right visualization panel."""
        panel = wx.Panel(parent)
        sizer = wx.BoxSizer(wx.VERTICAL)
        
        # Toolbar for view options
        tb = wx.ToolBar(panel, style=wx.TB_HORIZONTAL)
        tb.AddCheckTool(201, "Ratsnest", wx.NullBitmap, wx.NullBitmap,
                        shortHelp="Toggle ratsnest display")
        tb.Bind(wx.EVT_TOOL, self._on_toggle_ratsnest, id=201)
        tb.Realize()
        # Set initial state after Realize() (ToggleTool is the wx4 way)
        try:
            tb.ToggleTool(201, CONFIG.show_ratsnest)
        except Exception:
            pass
        self._ratsnest_toolbar = tb  # keep reference
        sizer.Add(tb, 0, wx.EXPAND)
        
        # Canvas
        self.canvas = PlacementPreviewCanvas(panel)
        sizer.Add(self.canvas, 1, wx.EXPAND)
        
        # Info panel
        self.info_text = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 100))
        sizer.Add(self.info_text, 0, wx.EXPAND | wx.ALL, 5)
        
        panel.SetSizer(sizer)
        return panel
    
    def _start_refresh_timer(self):
        """Start timer for async result checking."""
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_timer, self.timer)
        self.timer.Start(100)  # 100ms interval
    
    def _on_timer(self, event):
        """Check for async results."""
        completed = []
        for req_id in list(self.pending_requests):
            status, result = HTTP_CLIENT.get_result(req_id)
            if status != "pending":
                completed.append((req_id, status, result))
        
        for req_id, status, result in completed:
            self.pending_requests.discard(req_id)
            self._handle_async_result(req_id, status, result)
    
    def _handle_async_result(self, req_id: str, status: str, result: Any):
        """Handle completed async request."""
        req_type = self.request_types.pop(req_id, "")

        if status == "error":
            self.info_text.SetValue(f"Request failed:\n{result}")
            self.SetStatusText(f"Error: {result}")
            return
        
        if req_type == "optimize":
            self._apply_placement_result(result)
        elif req_type == "dfm":
            self._show_dfm_result(result)
        elif req_type == "generate":
            self._show_generate_result(result)
    
    def _extract_board_data(self):
        """Extract comprehensive board data — robust against KiCad API differences."""
        self.components = []
        self.nets = []

        # Extract components with detailed info
        try:
            footprints = list(self.board.GetFootprints())
        except Exception as exc:
            logger.error("GetFootprints failed: %s", exc)
            footprints = []

        for fp in footprints:
            try:
                pos = fp.GetPosition()

                # Get footprint dimensions (API varies across KiCad versions)
                try:
                    bbox = fp.GetBoundingBox(False, False)
                except TypeError:
                    try:
                        bbox = fp.GetBoundingBox(False)
                    except TypeError:
                        bbox = fp.GetBoundingBox()
                width = pcbnew.ToMM(bbox.GetWidth())
                height = pcbnew.ToMM(bbox.GetHeight())

                # Extract pins
                pins = []
                for pad in fp.GetPads():
                    try:
                        pad_pos = pad.GetPosition()
                        pins.append({
                            "number": pad.GetNumber(),
                            "net": pad.GetNetCode(),
                            "x": pcbnew.ToMM(pad_pos.x),
                            "y": pcbnew.ToMM(pad_pos.y),
                        })
                    except Exception:
                        pass

                comp = ComponentInfo(
                    ref=fp.GetReference(),
                    value=fp.GetValue(),
                    footprint=self._get_footprint_name(fp),
                    x=pcbnew.ToMM(pos.x),
                    y=pcbnew.ToMM(pos.y),
                    rotation=self._get_orientation_degrees(fp),
                    layer="top" if fp.GetLayer() == pcbnew.F_Cu else "bottom",
                    width=width,
                    height=height,
                    pins=pins,
                    is_fixed=fp.IsLocked(),
                )
                self.components.append(comp)
            except Exception as exc:
                logger.warning("Skipping footprint due to error: %s", exc)

        # Extract nets with classification
        try:
            net_info = self.board.GetNetInfo()
            nets_map = net_info.NetsByName()
            for net_name, net in nets_map.items():
                try:
                    if net.GetNetCode() == 0:
                        continue
                    net_type = self._classify_net(str(net_name))
                    pins = []
                    for pad in net.GetPads():
                        try:
                            pins.append({
                                "ref": pad.GetParent().GetReference(),
                                "pin": pad.GetNumber(),
                            })
                        except Exception:
                            pass
                    if len(pins) >= 2:
                        self.nets.append(NetInfo(
                            name=str(net_name),
                            code=net.GetNetCode(),
                            net_type=net_type,
                            pins=pins,
                        ))
                except Exception as exc:
                    logger.warning("Skipping net: %s", exc)
        except Exception as exc:
            logger.error("Net extraction failed: %s", exc)
        
        # Update UI
        self._update_component_list()
        self._update_canvas()
        self.SetStatusText(f"Loaded {len(self.components)} components, {len(self.nets)} nets")
    
    def _classify_net(self, name: str) -> NetType:
        """Classify net by name patterns."""
        name_upper = name.upper()
        
        # Power nets
        if any(p in name_upper for p in ["VCC", "VDD", "3V3", "5V", "1V8", "PWR"]):
            return NetType.POWER
        
        # Ground
        if any(g in name_upper for g in ["GND", "VSS", "AGND", "DGND"]):
            return NetType.GROUND
        
        # Clock
        if any(c in name_upper for c in ["CLK", "CLOCK", "OSC"]):
            return NetType.CLOCK
        
        # Differential (avoid false positives from single-char +/-)
        if any(d in name_upper for d in ["DIFF", "_DP", "_DN", "USB_D", "CAN_", "LVDS"]):
            return NetType.DIFFERENTIAL
        
        # High speed
        if any(h in name_upper for h in ["USB", "HDMI", "ETH", "LVDS"]):
            return NetType.HIGH_SPEED
        
        # Analog
        if any(a in name_upper for a in ["ADC", "DAC", "ANA", "SENSOR"]):
            return NetType.ANALOG
        
        return NetType.SIGNAL
    
    def _update_component_list(self):
        """Update the component list control."""
        self.comp_list.DeleteAllItems()
        for i, comp in enumerate(self.components):
            self.comp_list.InsertItem(i, comp.ref)
            self.comp_list.SetItem(i, 1, comp.value)
            self.comp_list.SetItem(i, 2, comp.footprint)
            self.comp_list.SetItem(i, 3, f"{comp.x:.1f}")
            self.comp_list.SetItem(i, 4, f"{comp.y:.1f}")
            self.comp_list.SetItem(i, 5, "Yes" if comp.is_fixed else "No")
    
    def _update_canvas(self):
        """Update the visualization canvas."""
        bbox = self.board.GetBoardEdgesBoundingBox()
        width_mm = pcbnew.ToMM(bbox.GetWidth())
        height_mm = pcbnew.ToMM(bbox.GetHeight())

        if width_mm <= 0 or height_mm <= 0:
            width_mm = max((c.x for c in self.components), default=100.0)
            height_mm = max((c.y for c in self.components), default=80.0)
            width_mm = max(40.0, width_mm * 1.2)
            height_mm = max(40.0, height_mm * 1.2)

        self.canvas.set_board_dimensions(
            width_mm,
            height_mm,
        )
        self.canvas.show_ratsnest = CONFIG.show_ratsnest
        self.canvas.update_components(self.components, self.nets)
    
    def _get_board_data_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for backend API."""
        bbox = self.board.GetBoardEdgesBoundingBox()
        board_width = pcbnew.ToMM(bbox.GetWidth())
        board_height = pcbnew.ToMM(bbox.GetHeight())
        if board_width <= 0:
            board_width = 100.0
        if board_height <= 0:
            board_height = 80.0

        return {
            "components": [
                {
                    "ref": c.ref,
                    "value": c.value,
                    "footprint": c.footprint,
                    "x": c.x,
                    "y": c.y,
                    "rotation": c.rotation,
                    "layer": c.layer,
                    "width": c.width,
                    "height": c.height,
                    "fixed": c.is_fixed,
                    "power_dissipation": c.power_dissipation,
                }
                for c in self.components
            ],
            "connections": [
                {
                    "net": n.name,
                    "net_type": n.net_type.name.lower(),
                    "pins": n.pins,
                }
                for n in self.nets
            ],
            "constraints": [
                {
                    "type": c.type,
                    "refs": c.refs,
                    "params": c.params,
                }
                for c in self.constraints if c.enabled
            ],
            "board_width": board_width,
            "board_height": board_height,
        }
    
    def _on_execute_prompt(self, event):
        """Execute natural language prompt."""
        prompt = self.prompt_ctrl.GetValue().strip()
        if not prompt:
            return
        
        self.SetStatusText("Processing request...")

        # /generate expects GenerateRequest, not BoardData
        data = {
            "prompt": prompt,
            "priority": "quality",
        }
        
        req_id = HTTP_CLIENT.request(
            f"{CONFIG.backend_url}/generate",
            data,
            callback=self._on_request_complete
        )
        self.pending_requests.add(req_id)
        self.request_types[req_id] = "generate"
    
    def _on_optimize_tool(self, event):
        """Run placement optimization."""
        self.SetStatusText("Optimizing placement...")
        
        data = self._get_board_data_dict()
        data["thermal_aware"] = CONFIG.thermal_aware
        
        req_id = HTTP_CLIENT.request(
            f"{CONFIG.backend_url}/placement/optimize?algorithm=auto",
            data,
            callback=self._on_request_complete
        )
        self.pending_requests.add(req_id)
        self.request_types[req_id] = "optimize"
    
    def _on_dfm_tool(self, event):
        """Run DFM check."""
        self.SetStatusText("Checking DFM...")
        
        data = self._get_board_data_dict()
        
        req_id = HTTP_CLIENT.request(
            f"{CONFIG.backend_url}/dfm/check",
            data,
            callback=self._on_request_complete
        )
        self.pending_requests.add(req_id)
        self.request_types[req_id] = "dfm"
    
    def _on_request_complete(self, request_id: str):
        """Called when async request completes."""
        # Handled in timer
        pass
    
    def _apply_placement_result(self, result: Dict):
        """Apply placement optimization results."""
        if not isinstance(result, dict):
            wx.MessageBox("Optimization failed: invalid response format", "Error")
            return

        if result.get("success") is False:
            wx.MessageBox(f"Optimization failed: {result.get('error')}", "Error")
            return
        
        positions = result.get("positions", {})
        if not positions:
            wx.MessageBox("Optimization completed but returned no positions.", "Info")
            return
        
        # Create undo point (API varies by KiCad version — skip silently if unavailable)
        try:
            pcbnew.SaveBoard(self.board.GetFileName(), self.board)
        except Exception:
            pass
        
        # Apply positions
        for fp in self.board.GetFootprints():
            ref = fp.GetReference()
            if ref in positions:
                pos = positions[ref]
                new_x = pcbnew.FromMM(pos["x"])
                new_y = pcbnew.FromMM(pos["y"])
                fp.SetPosition(pcbnew.VECTOR2I(new_x, new_y))
                
                if "rotation" in pos:
                    fp.SetOrientationDegrees(pos["rotation"])
        
        pcbnew.Refresh()
        
        # Update local data
        self._extract_board_data()
        
        # Show metrics
        metrics = result.get("metrics", {})
        info = (
            f"Optimization complete!\n\n"
            f"Wirelength: {metrics.get('wirelength', 0):.1f} mm\n"
            f"Thermal score: {metrics.get('thermal_score', 0):.1f}\n"
            f"Density: {metrics.get('density_score', 0):.1f}%\n"
            f"Time: {metrics.get('time_ms', 0):.0f} ms"
        )
        self.info_text.SetValue(info)
        self.SetStatusText("Optimization complete")
    
    def _show_dfm_result(self, result: Dict):
        """Display DFM check results."""
        if isinstance(result, list):
            violations = result
        elif isinstance(result, dict):
            violations = result.get("violations", [])
        else:
            violations = []

        if not violations:
            self.info_text.SetValue("✓ No DFM issues found!")
            return
        
        text = f"DFM Issues Found: {len(violations)}\n\n"
        for v in violations[:20]:
            sev = v.get('severity', 'warning').upper()
            text += f"[{sev}] {v.get('type', 'violation')}: {v.get('message', 'No details')}\n"
        
        self.info_text.SetValue(text)
        
        # Highlight violations on canvas
        for v in violations:
            for ref in v.get('components', []):
                self.canvas.highlight_component(ref, "red")

    def _show_generate_result(self, result: Any):
        """Display generate endpoint response in the info panel."""
        if not isinstance(result, dict):
            self.info_text.SetValue("Generate failed: invalid response format")
            return

        if not result.get("success"):
            self.info_text.SetValue(f"Generate failed: {result.get('error', 'Unknown error')}")
            self.SetStatusText("Generate failed")
            return

        circuit_data = result.get("circuit_data") or {}
        component_count = len(circuit_data.get("components", []))
        net_count = len(circuit_data.get("connections", []))

        summary = (
            f"Generation complete!\n\n"
            f"Mode: {result.get('generation_mode', 'llm')}\n"
            f"Source: {result.get('template_used', 'LLM/custom')}\n"
            f"Components: {component_count}\n"
            f"Nets: {net_count}\n"
            f"Time: {result.get('generation_time_ms', 0):.1f} ms\n"
            f"Download: {result.get('download_url', 'n/a')}"
        )
        self.info_text.SetValue(summary)
        self.SetStatusText("Generation complete")
    
    def _on_toggle_fixed(self, event):
        """Toggle fixed status of selected components."""
        selected = self._get_selected_component_refs()
        if not selected:
            wx.MessageBox("Select one or more components in the list first.", "Info")
            return

        updated = 0
        for fp in self.board.GetFootprints():
            if fp.GetReference() in selected:
                fp.SetLocked(not fp.IsLocked())
                updated += 1

        if updated:
            pcbnew.Refresh()
            self._extract_board_data()
            self.SetStatusText(f"Updated lock state for {updated} component(s)")
    
    def _on_add_constraint(self, event):
        """Add placement constraint."""
        refs = self._get_selected_component_refs()
        if not refs:
            wx.MessageBox("Select component(s) first to create a constraint.", "Info")
            return

        choices = ["fixed", "spacing"]
        with wx.SingleChoiceDialog(self, "Constraint type:", "Add Constraint", choices) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            ctype = choices[dlg.GetSelection()]

        params: Dict[str, Any] = {}
        if ctype == "spacing":
            value = wx.GetTextFromUser(
                "Minimum spacing in mm:",
                "Spacing Constraint",
                "1.0",
                parent=self,
            )
            if not value:
                return
            try:
                params["min_mm"] = max(0.1, float(value))
            except ValueError:
                wx.MessageBox("Invalid spacing value.", "Error", wx.OK | wx.ICON_ERROR)
                return

        constraint = Constraint(type=ctype, refs=refs, params=params)
        self.constraints.append(constraint)
        self._refresh_constraint_list()
        self.SetStatusText(f"Added {ctype} constraint for {', '.join(refs)}")
    
    def _on_settings(self, event):
        """Show settings dialog."""
        dialog = wx.Dialog(self, title="AI PCB Assistant Settings", size=(500, 300))
        panel = wx.Panel(dialog)
        sizer = wx.BoxSizer(wx.VERTICAL)

        sizer.Add(wx.StaticText(panel, label="Backend URL"), 0, wx.LEFT | wx.TOP, 10)
        txt_url = wx.TextCtrl(panel, value=CONFIG.backend_url)
        sizer.Add(txt_url, 0, wx.EXPAND | wx.ALL, 10)

        sizer.Add(wx.StaticText(panel, label="Request timeout (seconds)"), 0, wx.LEFT, 10)
        txt_timeout = wx.TextCtrl(panel, value=str(CONFIG.request_timeout))
        sizer.Add(txt_timeout, 0, wx.EXPAND | wx.ALL, 10)

        chk_thermal = wx.CheckBox(panel, label="Thermal-aware placement")
        chk_thermal.SetValue(CONFIG.thermal_aware)
        sizer.Add(chk_thermal, 0, wx.ALL, 10)

        chk_ratsnest = wx.CheckBox(panel, label="Show ratsnest in preview")
        chk_ratsnest.SetValue(CONFIG.show_ratsnest)
        sizer.Add(chk_ratsnest, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        btns = dialog.CreateButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(btns, 0, wx.EXPAND | wx.ALL, 10)
        panel.SetSizer(sizer)

        if dialog.ShowModal() == wx.ID_OK:
            CONFIG.backend_url = txt_url.GetValue().strip() or CONFIG.backend_url
            try:
                CONFIG.request_timeout = max(5, int(txt_timeout.GetValue().strip()))
            except ValueError:
                wx.MessageBox("Invalid timeout value. Keeping previous setting.", "Warning")
            CONFIG.thermal_aware = chk_thermal.GetValue()
            CONFIG.show_ratsnest = chk_ratsnest.GetValue()
            CONFIG.save()
            self.canvas.show_ratsnest = CONFIG.show_ratsnest
            self.canvas._draw()
            self.SetStatusText("Settings updated")

        dialog.Destroy()
    
    def _on_refresh(self, event):
        """Refresh board data."""
        self._extract_board_data()
    
    def _on_generate_tool(self, event):
        """Generate schematic from description."""
        prompt = wx.GetTextFromUser(
            "Describe the circuit to generate:",
            "AI Circuit Generation",
            "555 timer astable LED blinker",
            parent=self,
        )
        if not prompt:
            return
        self.prompt_ctrl.SetValue(prompt)
        self._on_execute_prompt(event)
    
    def _on_exit(self, event):
        self.Close()

    def _on_close(self, event):
        if hasattr(self, "timer") and self.timer.IsRunning():
            self.timer.Stop()
        event.Skip()

    def _on_toggle_ratsnest(self, event):
        CONFIG.show_ratsnest = bool(event.IsChecked())
        CONFIG.save()
        self.canvas.show_ratsnest = CONFIG.show_ratsnest
        self.canvas._draw()

    def _on_component_selected(self, event):
        ref = event.GetText()
        self.canvas.selected_refs = {ref}
        self.canvas._draw()
        self.SetStatusText(f"Selected {ref}")
        event.Skip()

    def _refresh_constraint_list(self):
        self.constraint_list.Clear()
        for idx, constraint in enumerate(self.constraints, start=1):
            refs = ", ".join(constraint.refs)
            suffix = ""
            if constraint.type == "spacing" and "min_mm" in constraint.params:
                suffix = f" (min={constraint.params['min_mm']} mm)"
            state = "on" if constraint.enabled else "off"
            self.constraint_list.Append(f"{idx}. [{state}] {constraint.type}: {refs}{suffix}")

    def _get_selected_component_refs(self) -> List[str]:
        refs: List[str] = []
        idx = self.comp_list.GetFirstSelected()
        while idx != -1:
            refs.append(self.comp_list.GetItemText(idx))
            idx = self.comp_list.GetNextSelected(idx)
        return refs

    @staticmethod
    def _get_footprint_name(fp: Any) -> str:
        """Return footprint name as a plain Python string."""
        try:
            fpid = fp.GetFPID()
            if hasattr(fpid, "GetLibItemName"):
                return str(fpid.GetLibItemName())
            if hasattr(fpid, "GetUniStringLibItemName"):
                return str(fpid.GetUniStringLibItemName())
        except Exception:
            pass
        return ""

    @staticmethod
    def _get_orientation_degrees(fp: Any) -> float:
        """Get footprint orientation in degrees — compatible with KiCad 7/8/9."""
        try:
            # KiCad 7+ returns EDA_ANGLE
            return fp.GetOrientation().AsDegrees()
        except AttributeError:
            pass
        try:
            # Older API
            return fp.GetOrientationDegrees()
        except AttributeError:
            pass
        try:
            return float(fp.GetOrientation()) / 10.0  # tenths of degree
        except Exception:
            return 0.0


# ── Dashboard Dialog ──────────────────────────────────────────────────────────

class AIDashboardDialog(wx.Frame):
    """Dashboard-first launcher styled after the prototype plugin UI."""

    def __init__(self, parent, plugin: AIPlacementPlugin, board: pcbnew.BOARD):
        super().__init__(parent, title="AI KiCad Plugin", size=(460, 760), style=wx.DEFAULT_FRAME_STYLE | wx.STAY_ON_TOP)
        self.plugin = plugin
        self.board = board
        self.SetBackgroundColour(wx.Colour(26, 26, 30))
        self._init_ui()
        self._refresh_health()

    def _init_ui(self):
        panel = wx.Panel(self)
        panel.SetBackgroundColour(wx.Colour(26, 26, 30))
        vbox = wx.BoxSizer(wx.VERTICAL)

        title = wx.StaticText(panel, label="AI-Powered KiCad Plugin")
        title.SetForegroundColour(wx.Colour(0, 210, 255))
        title.SetFont(wx.Font(16, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        vbox.Add(title, 0, wx.ALIGN_CENTER | wx.TOP, 22)

        subtitle = wx.StaticText(panel, label="Local AI • No Cloud • 100% Private")
        subtitle.SetForegroundColour(wx.Colour(185, 185, 190))
        subtitle.SetFont(wx.Font(10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_ITALIC, wx.FONTWEIGHT_NORMAL))
        vbox.Add(subtitle, 0, wx.ALIGN_CENTER | wx.TOP, 4)

        line = wx.StaticLine(panel)
        vbox.Add(line, 0, wx.EXPAND | wx.ALL, 16)

        self.btn_generate = self._action_button(panel, "Auto Generate Schematic", wx.Colour(0, 120, 220))
        self.btn_write = self._action_button(panel, "Write Components to PCB", wx.Colour(0, 180, 180))
        self.btn_netlist = self._action_button(panel, "Generate Netlist", wx.Colour(40, 70, 220))
        self.btn_place = self._action_button(panel, "AI Component Placement (RL)", wx.Colour(0, 165, 95))
        self.btn_route = self._action_button(panel, "FreeRouting Autoroute", wx.Colour(0, 145, 155))
        self.btn_mfg = self._action_button(panel, "Manufacturing Checks", wx.Colour(220, 125, 0))
        self.btn_drc = self._action_button(panel, "Run DRC Check", wx.Colour(165, 45, 180))
        self.btn_gerber = self._action_button(panel, "Export Gerber Files", wx.Colour(190, 55, 55))

        for btn in (self.btn_generate, self.btn_write, self.btn_netlist, self.btn_place, self.btn_route, self.btn_mfg, self.btn_drc, self.btn_gerber):
            vbox.Add(btn, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 16)

        self.btn_generate.Bind(wx.EVT_BUTTON, self._on_generate)
        self.btn_write.Bind(wx.EVT_BUTTON, self._on_write_components)
        self.btn_netlist.Bind(wx.EVT_BUTTON, self._on_netlist)
        self.btn_place.Bind(wx.EVT_BUTTON, self._on_placement)
        self.btn_route.Bind(wx.EVT_BUTTON, self._on_freerouting)
        self.btn_mfg.Bind(wx.EVT_BUTTON, self._on_dfm)
        self.btn_drc.Bind(wx.EVT_BUTTON, self._on_drc)
        self.btn_gerber.Bind(wx.EVT_BUTTON, self._on_export_gerbers)

        self.status = wx.StaticText(panel, label="Checking backend status...")
        self.status.SetForegroundColour(wx.Colour(0, 210, 110))
        self.status.SetFont(wx.Font(10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        vbox.Add(self.status, 0, wx.ALIGN_CENTER | wx.TOP, 18)

        footer = wx.StaticText(panel, label="Powered by the updated AI PCB Assistant backend")
        footer.SetForegroundColour(wx.Colour(120, 120, 130))
        footer.SetFont(wx.Font(8, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_ITALIC, wx.FONTWEIGHT_NORMAL))
        vbox.Add(footer, 0, wx.ALIGN_CENTER | wx.TOP | wx.BOTTOM, 12)

        panel.SetSizer(vbox)

    def _action_button(self, parent, label: str, color: wx.Colour) -> wx.Button:
        btn = wx.Button(parent, label=label, size=(-1, 46))
        btn.SetBackgroundColour(color)
        btn.SetForegroundColour(wx.WHITE)
        btn.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        return btn

    def _refresh_health(self):
        try:
            req = urllib.request.Request(f"{CONFIG.backend_url}/health", method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
            mode = "healthy" if data.get("llm_loaded") else "running"
            self._set_status(f"Ready — FastAPI + backend {mode}", (0, 210, 110))
        except Exception as exc:
            self._set_status(f"Backend unavailable — {exc}", (255, 120, 120))

    def _set_status(self, message: str, color=(0, 210, 110)):
        self.status.SetLabel(message)
        self.status.SetForegroundColour(wx.Colour(*color))
        self.status.Refresh()

    def _prompt_dialog(self, title: str, message: str, default: str) -> Optional[str]:
        dlg = wx.TextEntryDialog(self, message, title, default)
        value = None
        if dlg.ShowModal() == wx.ID_OK:
            value = dlg.GetValue().strip()
        dlg.Destroy()
        return value or None

    def _post_json(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        req = urllib.request.Request(
            f"{CONFIG.backend_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=CONFIG.request_timeout) as resp:
            return json.loads(resp.read().decode())

    def _show_text(self, title: str, text: str):
        dlg = wx.MessageDialog(self, text, title, wx.OK | wx.ICON_INFORMATION)
        dlg.ShowModal()
        dlg.Destroy()

    def _select_freerouting_jar(self) -> Optional[str]:
        start_dir = os.path.dirname(CONFIG.freerouting_jar) if CONFIG.freerouting_jar else os.path.expanduser("~")
        with wx.FileDialog(
            self,
            "Select FreeRouting JAR",
            defaultDir=start_dir,
            wildcard="JAR files (*.jar)|*.jar",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                path = dlg.GetPath()
                CONFIG.freerouting_jar = path
                CONFIG.save()
                return path
        return None

    def _ensure_freerouting_jar(self) -> str:
        jar = (CONFIG.freerouting_jar or "").strip()
        if jar and os.path.exists(jar):
            return jar
        chosen = self._select_freerouting_jar()
        if chosen and os.path.exists(chosen):
            return chosen
        raise RuntimeError(
            "FreeRouting JAR not configured. Download freerouting.jar and select it when prompted."
        )

    def _call_first_available(self, names: List[str], *args):
        errors = []
        for name in names:
            fn = getattr(pcbnew, name, None)
            if not callable(fn):
                continue
            try:
                return fn(*args)
            except TypeError as exc:
                errors.append(f"{name}: {exc}")
                continue
        if errors:
            raise RuntimeError("; ".join(errors))
        raise RuntimeError(f"KiCad API not available for any of: {', '.join(names)}")

    def _export_dsn(self, dsn_path: str):
        board_path = self.board.GetFileName() if self.board else ""
        errors = []
        candidates = [
            ("ExportSpecctraDSN", (self.board, dsn_path)),
            ("ExportSpecctraDSN", (dsn_path,)),
            ("ExportDSN", (self.board, dsn_path)),
            ("ExportDSN", (dsn_path,)),
        ]
        for name, args in candidates:
            fn = getattr(pcbnew, name, None)
            if not callable(fn):
                continue
            try:
                fn(*args)
                if os.path.exists(dsn_path):
                    return
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        raise RuntimeError(
            "Unable to export DSN from KiCad. Save the board first and verify your KiCad build supports Specctra export."
            + (f" Details: {' | '.join(errors)}" if errors else "")
            + (f" Board: {board_path}" if board_path else "")
        )

    def _import_ses(self, ses_path: str) -> bool:
        errors = []
        candidates = [
            ("ImportSpecctraSES", (self.board, ses_path)),
            ("ImportSpecctraSES", (ses_path,)),
            ("ImportSpecctraSession", (self.board, ses_path)),
            ("ImportSpecctraSession", (ses_path,)),
            ("LoadSpecctraSession", (self.board, ses_path)),
            ("LoadSpecctraSession", (ses_path,)),
        ]
        for name, args in candidates:
            fn = getattr(pcbnew, name, None)
            if not callable(fn):
                continue
            try:
                fn(*args)
                try:
                    pcbnew.Refresh()
                except Exception:
                    pass
                return True
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        logger.warning("SES import not available: %s", " | ".join(errors))
        return False

    def _export_gerbers(self, output_dir: str) -> List[str]:
        os.makedirs(output_dir, exist_ok=True)
        generated: List[str] = []
        plot_controller = pcbnew.PLOT_CONTROLLER(self.board)
        options = plot_controller.GetPlotOptions()
        options.SetOutputDirectory(output_dir)
        options.SetPlotFrameRef(False)
        options.SetAutoScale(False)
        options.SetScale(1)
        options.SetMirror(False)
        options.SetUseGerberAttributes(True)
        try:
            options.SetUseGerberX2format(True)
        except Exception:
            pass
        try:
            options.SetSubtractMaskFromSilk(False)
        except Exception:
            pass

        layers = [
            ("F_Cu", pcbnew.F_Cu, "Front copper"),
            ("B_Cu", pcbnew.B_Cu, "Back copper"),
            ("F_SilkS", pcbnew.F_SilkS, "Front silkscreen"),
            ("B_SilkS", pcbnew.B_SilkS, "Back silkscreen"),
            ("F_Mask", pcbnew.F_Mask, "Front solder mask"),
            ("B_Mask", pcbnew.B_Mask, "Back solder mask"),
            ("Edge_Cuts", pcbnew.Edge_Cuts, "Board outline"),
        ]

        for suffix, layer_id, description in layers:
            plot_controller.SetLayer(layer_id)
            plot_controller.OpenPlotfile(suffix, pcbnew.PLOT_FORMAT_GERBER, description)
            if plot_controller.PlotLayer():
                generated.append(os.path.join(output_dir, f"{suffix}.gbr"))
        plot_controller.ClosePlot()

        excellon = getattr(pcbnew, "EXCELLON_WRITER", None)
        if excellon is not None:
            try:
                drill_writer = excellon(self.board)
                try:
                    drill_writer.SetFormat(True)
                except Exception:
                    pass
                try:
                    drill_writer.SetOptions(False, False, output_dir, False)
                except TypeError:
                    try:
                        drill_writer.SetOptions(False, False, output_dir)
                    except Exception:
                        pass
                try:
                    drill_writer.CreateDrillandMapFilesSet(output_dir, True, False)
                except TypeError:
                    drill_writer.CreateDrillandMapFilesSet(output_dir, True, False, False)
            except Exception as exc:
                logger.warning("Excellon drill export failed: %s", exc)

        return generated

    def _on_export_gerbers(self, event):
        board_file = self.board.GetFileName() if self.board else ""
        if not board_file:
            self._show_text("Export Gerber Files", "Save the PCB board first before exporting Gerbers.")
            return

        board_dir = os.path.dirname(board_file) or os.getcwd()
        default_dir = os.path.join(board_dir, "gerbers")
        with wx.DirDialog(self, "Choose Gerber output folder", defaultPath=default_dir, style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                output_dir = dlg.GetPath()
            else:
                return

        try:
            self._set_status("Exporting Gerber files...", (255, 210, 90))
            generated = self._export_gerbers(output_dir)
            summary = [f"Gerber output: {output_dir}"]
            if generated:
                summary.append("")
                summary.append("Layers exported:")
                summary.extend(os.path.basename(path) for path in generated)
            summary.append("")
            summary.append("Drill files are exported when KiCad's Excellon writer is available.")
            self._set_status("Gerber export complete.", (0, 210, 110))
            self._show_text("Export Gerber Files", "\n".join(summary))
        except Exception as exc:
            self._set_status("Gerber export failed.", (255, 120, 120))
            self._show_text("Export Gerber Files", str(exc))

    def _on_generate(self, event):
        prompt = self._prompt_dialog(
            "Auto Generate Schematic",
            "Describe the circuit to generate:",
            "12V to 3.3V regulator for sensor board with status LED",
        )
        if not prompt:
            return
        self._set_status("Generating schematic...", (255, 210, 90))
        try:
            result = self._post_json("/generate", {"prompt": prompt, "priority": "quality"})
            if not result.get("success"):
                raise RuntimeError(result.get("error", "Unknown backend error"))
            circuit_data = result.get("circuit_data") or {}
            download_url = result.get('download_url')
            
            if download_url:
                full_url = f"{CONFIG.backend_url}{download_url}"
                req = urllib.request.Request(full_url, method="GET")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    sch_data = resp.read()
                
                default_name = download_url.split('/')[-1]
                default_dir = os.path.dirname(self.board.GetFileName()) if self.board and self.board.GetFileName() else ""
                
                with wx.FileDialog(self, "Save Generated Schematic", defaultDir=default_dir,
                                   defaultFile=default_name, wildcard="KiCad Schematic (*.kicad_sch)|*.kicad_sch",
                                   style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dlg:
                    if dlg.ShowModal() == wx.ID_OK:
                        save_path = dlg.GetPath()
                        with open(save_path, 'wb') as f:
                            f.write(sch_data)
                        
                        summary = (
                            f"Schematic successfully saved to:\n{save_path}\n\n"
                            f"Mode: {result.get('generation_mode', 'llm')}\n"
                            f"Components: {len(circuit_data.get('components', []))}\n"
                            f"Nets: {len(circuit_data.get('connections', []))}"
                        )
                        self._set_status("Schematic saved.", (0, 210, 110))
                        
                        # Show success and offer to open
                        dlg2 = wx.MessageDialog(self, summary + "\n\nWould you like to open it now?", "Schematic Saved", wx.YES_NO | wx.ICON_INFORMATION)
                        if dlg2.ShowModal() == wx.ID_YES:
                            import platform, subprocess
                            if platform.system() == 'Darwin':
                                subprocess.Popen(['open', save_path])
                            elif platform.system() == 'Windows':
                                os.startfile(save_path)
                            else:
                                subprocess.Popen(['xdg-open', save_path])
                        dlg2.Destroy()
                    else:
                        self._set_status("Save cancelled.", (200, 200, 0))
            else:
                summary = (
                    f"Mode: {result.get('generation_mode', 'llm')}\n"
                    f"Components: {len(circuit_data.get('components', []))}\n"
                )
                self._set_status("Schematic generated (no download url).", (200, 200, 0))
                self._show_text("Schematic Generated", summary)
        except Exception as exc:
            self._set_status("Generation failed.", (255, 120, 120))
            self._show_text("Error", str(exc))

    def _classify_net(self, name: str) -> str:
        name_upper = (name or "").upper()
        if any(p in name_upper for p in ["VCC", "VDD", "3V3", "5V", "12V", "VIN", "PWR"]):
            return "power"
        if any(g in name_upper for g in ["GND", "VSS", "AGND", "DGND"]):
            return "ground"
        if any(c in name_upper for c in ["CLK", "CLOCK", "OSC"]):
            return "clock"
        if any(a in name_upper for a in ["ADC", "DAC", "SENSE", "ANALOG"]):
            return "analog"
        return "signal"

    def _collect_board_data(self) -> Dict[str, Any]:
        components: List[Dict[str, Any]] = []
        connections: List[Dict[str, Any]] = []
        try:
            bbox = self.board.GetBoardEdgesBoundingBox()
            board_width = pcbnew.ToMM(bbox.GetWidth())
            board_height = pcbnew.ToMM(bbox.GetHeight())
        except Exception:
            board_width = 100.0
            board_height = 80.0

        try:
            footprints = list(self.board.GetFootprints())
        except Exception:
            footprints = []

        for fp in footprints:
            try:
                pos = fp.GetPosition()
                try:
                    bbox = fp.GetBoundingBox(False, False)
                except TypeError:
                    try:
                        bbox = fp.GetBoundingBox(False)
                    except TypeError:
                        bbox = fp.GetBoundingBox()

                components.append(
                    {
                        "ref": fp.GetReference(),
                        "value": fp.GetValue(),
                        "footprint": AIPCBFrame._get_footprint_name(fp),
                        "x": pcbnew.ToMM(pos.x),
                        "y": pcbnew.ToMM(pos.y),
                        "rotation": AIPCBFrame._get_orientation_degrees(fp),
                        "layer": "top" if fp.GetLayer() == pcbnew.F_Cu else "bottom",
                        "width": pcbnew.ToMM(bbox.GetWidth()),
                        "height": pcbnew.ToMM(bbox.GetHeight()),
                        "fixed": bool(fp.IsLocked()),
                        "power_dissipation": 0.0,
                    }
                )
            except Exception:
                continue

        try:
            nets_map = self.board.GetNetInfo().NetsByName()
            for net_name, net in nets_map.items():
                try:
                    if net.GetNetCode() == 0:
                        continue
                    pins = []
                    for pad in net.GetPads():
                        try:
                            pins.append({"ref": pad.GetParent().GetReference(), "pin": str(pad.GetNumber())})
                        except Exception:
                            pass
                    if len(pins) >= 2:
                        connections.append(
                            {
                                "net": str(net_name),
                                "net_type": self._classify_net(str(net_name)),
                                "pins": pins,
                            }
                        )
                except Exception:
                    continue
        except Exception:
            pass

        if board_width <= 0:
            board_width = 100.0
        if board_height <= 0:
            board_height = 80.0

        return {
            "components": components,
            "connections": connections,
            "constraints": [],
            "board_width": board_width,
            "board_height": board_height,
        }

    def _apply_positions(self, result: Dict[str, Any]) -> int:
        positions = result.get("positions", {}) if isinstance(result, dict) else {}
        moved = 0
        if not positions:
            return moved
        for fp in self.board.GetFootprints():
            ref = fp.GetReference()
            pos = positions.get(ref)
            if not pos:
                continue
            try:
                fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(pos["x"]), pcbnew.FromMM(pos["y"])))
                if "rotation" in pos:
                    try:
                        fp.SetOrientationDegrees(pos["rotation"])
                    except Exception:
                        pass
                moved += 1
            except Exception:
                continue
        if moved:
            try:
                pcbnew.Refresh()
            except Exception:
                pass
        return moved

    def _project_schematic_path(self) -> Optional[str]:
        board_file = self.board.GetFileName() if self.board else ""
        if not board_file:
            return None
        candidate = os.path.splitext(board_file)[0] + ".kicad_sch"
        return candidate if os.path.exists(candidate) else None

    def _on_write_components(self, event):
        board_file = self.board.GetFileName() if self.board else ""
        if not board_file:
            self._show_text("Write Components to PCB", "Save the PCB project first so KiCad can link it with the project schematic.")
            return

        data = self._collect_board_data()
        schematic_path = self._project_schematic_path()
        lines = [f"PCB file: {board_file}"]
        if schematic_path:
            lines.append(f"Project schematic: {schematic_path}")
        else:
            lines.append("Project schematic: not found next to the board")

        if data["components"]:
            lines.append("")
            lines.append(f"The PCB already contains {len(data['components'])} footprint(s).")
            lines.append("If you changed the schematic, run KiCad: Tools > Update PCB from Schematic.")
        else:
            lines.append("")
            lines.append("No footprints are currently on the PCB.")
            lines.append("To write components to the PCB:")
            lines.append("1. Open the project schematic in Schematic Editor")
            lines.append("2. Assign footprints to symbols")
            lines.append("3. In PCB Editor, run Tools > Update PCB from Schematic")
            lines.append("4. Save the .kicad_pcb board and rerun placement/routing")

        self._set_status("Checked PCB write workflow.", (0, 210, 110))
        self._show_text("Write Components to PCB", "\n".join(lines))

    def _on_netlist(self, event):
        prompt = self._prompt_dialog(
            "Generate Netlist",
            "Describe the circuit to summarize as a netlist:",
            "op amp buffer for analog sensor output",
        )
        if not prompt:
            return
        self._set_status("Generating netlist summary...", (255, 210, 90))
        try:
            result = self._post_json("/generate", {"prompt": prompt, "priority": "quality"})
            if not result.get("success"):
                raise RuntimeError(result.get("error", "Unknown backend error"))
            connections = (result.get("circuit_data") or {}).get("connections", [])
            lines = []
            for conn in connections[:12]:
                net_name = conn.get('net')
                pin_str = ', '.join([f"{p.get('ref')}.{p.get('pin')}" for p in conn.get('pins', [])])
                lines.append(f"{net_name}: {pin_str}")
            text = "Netlist summary\n\n" + ("\n".join(lines) if lines else "No nets returned")
            self._set_status("Netlist generated.", (0, 210, 110))
            self._show_text("Generate Netlist", text)
        except Exception as exc:
            self._set_status("Netlist generation failed.", (255, 120, 120))
            self._show_text("Error", str(exc))

    def _on_placement(self, event):
        self._set_status("Running placement optimization...", (255, 210, 90))
        try:
            data = self._collect_board_data()
            if not data["components"]:
                raise RuntimeError("No footprints found on the current board.")
            data["thermal_aware"] = CONFIG.thermal_aware
            result = self._post_json("/placement/optimize?algorithm=auto", data)
            moved = self._apply_positions(result)
            metrics = result.get("metrics", {}) if isinstance(result, dict) else {}
            summary = (
                f"Moved footprints: {moved}\n"
                f"Wirelength: {metrics.get('wirelength', 0):.1f} mm\n"
                f"Thermal score: {metrics.get('thermal_score', 0):.1f}\n"
                f"Density: {metrics.get('density_score', 0):.1f}%"
            )
            self._set_status("Placement optimization complete.", (0, 210, 110))
            self._show_text("AI Component Placement", summary)
        except Exception as exc:
            self._set_status("Placement failed.", (255, 120, 120))
            self._show_text("Error", str(exc))

    def _on_freerouting(self, event):
        board_file = self.board.GetFileName() if self.board else ""
        if not board_file:
            self._show_text("FreeRouting Autoroute", "Save the PCB board first before running FreeRouting.")
            return

        try:
            jar_path = self._ensure_freerouting_jar()
            board_dir = os.path.dirname(board_file) or os.getcwd()
            board_base = os.path.splitext(os.path.basename(board_file))[0]
            dsn_path = os.path.join(board_dir, f"{board_base}.dsn")
            ses_path = os.path.join(board_dir, f"{board_base}.ses")

            self._set_status("Exporting DSN for FreeRouting...", (255, 210, 90))
            self._export_dsn(dsn_path)

            self._set_status("Running FreeRouting...", (255, 210, 90))
            cmd = ["java", "-jar", jar_path, "-de", dsn_path, "-do", ses_path]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=max(60, int(CONFIG.freerouting_timeout)),
            )
            if proc.returncode != 0:
                stderr = (proc.stderr or proc.stdout or "Unknown FreeRouting error").strip()
                raise RuntimeError(stderr[-1200:])

            if not os.path.exists(ses_path):
                raise RuntimeError("FreeRouting finished but no SES session file was produced.")

            imported = self._import_ses(ses_path)
            summary = (
                f"DSN: {dsn_path}\n"
                f"SES: {ses_path}\n"
                f"Imported into KiCad: {'Yes' if imported else 'No'}"
            )
            if not imported:
                summary += "\n\nImport the SES manually in KiCad via File > Import > SES."

            self._set_status("FreeRouting completed.", (0, 210, 110))
            self._show_text("FreeRouting Autoroute", summary)
        except subprocess.TimeoutExpired:
            self._set_status("FreeRouting timed out.", (255, 120, 120))
            self._show_text(
                "FreeRouting Autoroute",
                "FreeRouting timed out before finishing. Try a simpler board or increase freerouting_timeout in config.",
            )
        except Exception as exc:
            self._set_status("FreeRouting failed.", (255, 120, 120))
            self._show_text("FreeRouting Autoroute", str(exc))

    def _on_dfm(self, event):
        self._run_board_check("/dfm/check", "Manufacturing Checks")

    def _on_drc(self, event):
        self._run_board_check("/dfm/check", "Run DRC Check")

    def _run_board_check(self, path: str, title: str):
        self._set_status(f"Running {title.lower()}...", (255, 210, 90))
        try:
            data = self._collect_board_data()
            if not data["components"]:
                raise RuntimeError("No footprints found on the current board.")
            result = self._post_json(path, data)
            violations = result if isinstance(result, list) else result.get("violations", [])
            if not violations:
                text = "No issues found."
            else:
                lines = [f"[{v.get('severity', 'warning').upper()}] {v.get('message', 'No details')}" for v in violations[:12]]
                text = "\n".join(lines)
            self._set_status(f"{title} complete.", (0, 210, 110))
            self._show_text(title, text)
        except Exception as exc:
            self._set_status(f"{title} failed.", (255, 120, 120))
            self._show_text("Error", str(exc))


# ── Legacy Dialog for compatibility ───────────────────────────────────────────

class AIAssistantDialog(wx.Dialog):
    """Legacy dialog for simple usage."""
    
    def __init__(self, parent, board):
        super().__init__(parent, title="AI PCB Assistant", size=(500, 400))
        self.board = board
        self._init_ui()
    
    def _init_ui(self):
        # Simple UI for basic usage
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        
        sizer.Add(wx.StaticText(panel, label="Use the full AI PCB Assistant window\n"
                                           "for advanced features."), 0, wx.ALL, 20)
        
        btn_open = wx.Button(panel, label="Open Full Interface")
        btn_open.Bind(wx.EVT_BUTTON, self._on_open_full)
        sizer.Add(btn_open, 0, wx.ALIGN_CENTER | wx.ALL, 10)
        
        panel.SetSizer(sizer)
    
    def _on_open_full(self, event):
        self.EndModal(wx.ID_OK)
        frame = AIPCBFrame(None, self.board)
        frame.Show()
