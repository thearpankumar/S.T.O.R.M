import asyncio
import logging
import threading
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar
import re

import nest_asyncio
import streamlit as st

from config.domains import CYBERSECURITY_DOMAINS
from config.settings import settings
from db.store import (
    db,
    get_domain_id,
    get_subdomains,
    init_db,
    shutdown_db,
    update_subdomain_status,
    get_tools,
    get_features,
)
from models.worker import WorkerEvent
from orchestrator.graph import create_event_queue, run_worker_pipeline

from tui.actions.pipeline import register_all_actions
from tui.components.domain_tree import render_domain_tree, get_selection_count
from tui.components.detail_panel import render_detail_panel
from tui.components.bulk_actions import render_bulk_action_bar
from tui.utils.icons import icon_html, status_icon

T = TypeVar("T")

logger = logging.getLogger(__name__)

nest_asyncio.apply()

register_all_actions()

import concurrent.futures

_PIPELINE_FUTURES: dict[str, concurrent.futures.Future] = {}

@st.cache_resource
def get_executor() -> concurrent.futures.ThreadPoolExecutor:
    return concurrent.futures.ThreadPoolExecutor(max_workers=settings.max_workers, thread_name_prefix="pipeline_worker")

_EXECUTOR = get_executor()

import atexit
def _shutdown_executor() -> None:
    _EXECUTOR.shutdown(wait=False, cancel_futures=True)
atexit.register(_shutdown_executor)

@st.cache_resource
def get_pipeline_progress() -> dict[str, dict]:
    return {}

@st.cache_resource
def get_pipeline_lock() -> threading.Lock:
    return threading.Lock()

_PIPELINE_PROGRESS = get_pipeline_progress()
_PIPELINE_LOCK = get_pipeline_lock()

# Per-pipeline stop signals — set() to request graceful cancellation
_PIPELINE_STOP_FLAGS: dict[str, threading.Event] = {}


def init_session_state() -> None:
    import copy
    defaults = {
        "initialized": False,
        "event_queue": None,
        "running_pipelines": {},
        "status_message": "",
        "status_type": "info",
        "selected_items": set(),
        "expanded_domains": {},
        "detail_item": None,
        "search_query": "",
        "context_menu_target": None,
        "t2_selected_domain": None,
        "t2_selected_tool": None,
        "last_export_path": None,
        # ── T3 ─────────────────────────────────────────
        "t3_selected_tool_id": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = copy.deepcopy(value)


_app_thread_locals = threading.local()

def get_event_loop() -> asyncio.AbstractEventLoop:
    try:
        loop = asyncio.get_running_loop()
        return loop
    except RuntimeError:
        loop = getattr(_app_thread_locals, "loop", None)
        if loop is None or loop.is_closed():
            loop = asyncio.new_event_loop()
            _app_thread_locals.loop = loop
        asyncio.set_event_loop(loop)
        return loop

def run_async(coro: Coroutine[Any, Any, T]) -> T:
    try:
        loop = asyncio.get_running_loop()
        return loop.run_until_complete(coro)
    except RuntimeError:
        loop = get_event_loop()
        return loop.run_until_complete(coro)


async def init_app_async() -> None:
    if not st.session_state.initialized:
        Path(settings.log_dir).mkdir(parents=True, exist_ok=True)
        await init_db()
        try:
            from tools.router import quota_manager
            await quota_manager.initialize()
        except ImportError:
            logger.debug("quota_manager not available")
        st.session_state.event_queue = create_event_queue()
        st.session_state.initialized = True


async def shutdown_app_async() -> None:
    if st.session_state.initialized:
        await shutdown_db()
        st.session_state.initialized = False


async def load_domain_tree_async() -> dict[str, list[dict]]:
    tree_data = {}
    for domain in CYBERSECURITY_DOMAINS:
        domain_id = await get_domain_id(domain)
        if domain_id:
            subdomains = await get_subdomains(domain_id)
            tree_data[domain] = subdomains
        else:
            logger.warning(f"Domain '{domain}' not found in database")
    return tree_data


async def discover_subdomains_async(domain: str) -> list[dict]:
    try:
        from agents.discovery import discover_subdomains
        return await discover_subdomains(domain)
    except ImportError as e:
        logger.error(f"Failed to import discovery module: {e}")
        raise RuntimeError("Discovery module not available") from e


def cleanup_completed_pipelines() -> None:
    keys_to_remove = [
        k for k, v in st.session_state.running_pipelines.items()
        if v.get("status") in ("done", "failed")
    ]
    for k in keys_to_remove:
        del st.session_state.running_pipelines[k]
    # Also prune the module-level tracker
    with _PIPELINE_LOCK:
        for k in list(_PIPELINE_PROGRESS):
            if _PIPELINE_PROGRESS[k].get("status") in ("done", "failed"):
                del _PIPELINE_PROGRESS[k]


def sync_pipeline_state() -> None:
    """Copy live progress from _PIPELINE_PROGRESS into st.session_state.running_pipelines.

    Called once per Streamlit render cycle so the UI always reflects the
    latest state written by background threads.
    """
    import copy
    with _PIPELINE_LOCK:
        for key in list(_PIPELINE_PROGRESS.keys()):
            data = _PIPELINE_PROGRESS.get(key)
            if data:
                st.session_state.running_pipelines[key] = copy.deepcopy(data)


async def run_pipeline_async(
    domain: str,
    subdomain_id: int,
    subdomain_name: str,
    event_queue: Any,          # passed explicitly so background thread can use it
) -> None:
    """Run the worker pipeline and track progress in the module-level dict.

    This function must NOT touch st.session_state — it runs in a background
    thread where Streamlit's ScriptRunContext is unavailable.
    """
    pipeline_key = f"{domain}_{subdomain_id}"
    pending_events: list[WorkerEvent] = []

    def _update(patch: dict) -> None:
        with _PIPELINE_LOCK:
            if pipeline_key in _PIPELINE_PROGRESS:
                _PIPELINE_PROGRESS[pipeline_key].update(patch)

    task: asyncio.Task | None = None
    try:
        task = asyncio.create_task(
            run_worker_pipeline(domain, subdomain_id, subdomain_name, event_queue)
        )

        while not task.done():
            try:
                event = await asyncio.wait_for(
                    event_queue.get(), timeout=settings.event_timeout
                )
                if event.subdomain == subdomain_name:
                    _update({
                        "progress": event.progress_pct,
                        "step":     event.step,
                        "message":  event.message,
                        "status":   "running",
                    })
                else:
                    pending_events.append(event)
            except asyncio.TimeoutError:
                continue

        for ev in pending_events:
            try:
                await event_queue.put(ev)
            except Exception as queue_err:
                logger.error(f"Failed to re-queue event for subdomain {ev.subdomain}: {queue_err}")

        _update({"status": "done", "progress": 1.0, "message": "Completed"})

    except Exception as e:
        logger.error(f"Pipeline failed for {subdomain_name}: {e}")
        _update({"status": "failed", "message": str(e)})
        try:
            await update_subdomain_status(subdomain_id, "failed")
        except Exception as db_err:
            logger.warning(f"Failed to update DB status: {db_err}")

        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        for ev in pending_events:
            try:
                await event_queue.put(ev)
            except Exception as queue_err:
                logger.error(f"Failed to re-queue event during cleanup for {ev.subdomain}: {queue_err}")


async def export_subdomain_async(subdomain_id: int, subdomain_name: str) -> str:
    try:
        from excel.bridge import create_workbook_from_db
        await create_workbook_from_db(subdomain_id, subdomain_name)
        return settings.excel_output_path
    except ImportError as e:
        logger.error(f"Failed to import excel bridge: {e}")
        raise RuntimeError("Excel export module not available") from e


async def export_t2_subdomain_async(subdomain_id: int, subdomain_name: str) -> str:
    try:
        from excel.t2_bridge import create_t2_workbook_from_db
        output_path = await create_t2_workbook_from_db(subdomain_id, subdomain_name)
        return output_path
    except ImportError as e:
        logger.error(f"Failed to import excel t2_bridge: {e}")
        raise RuntimeError("Excel T2 export module not available") from e


async def export_all_t2_async() -> str:
    try:
        from excel.t2_bridge import export_all_t2_subdomains
        return await export_all_t2_subdomains()
    except ImportError as e:
        logger.error(f"Failed to import excel t2_bridge: {e}")
        raise RuntimeError("Excel T2 export module not available") from e


async def export_all_async() -> str:
    try:
        from excel.bridge import export_all_subdomains
        return await export_all_subdomains()
    except ImportError as e:
        logger.error(f"Failed to import excel bridge: {e}")
        raise RuntimeError("Excel export module not available") from e


async def export_t3_async() -> str:
    try:
        from excel.t3_bridge import export_t3_workbook
        return await export_t3_workbook()
    except ImportError as e:
        logger.error(f"Failed to import excel t3_bridge: {e}")
        raise RuntimeError("Excel T3 export module not available") from e
    except ValueError as e:
        # export_t3_workbook raises ValueError when no tools exist yet
        raise RuntimeError(str(e)) from e
    except Exception as e:
        logger.error(f"T3 export failed: {e}")
        raise RuntimeError(f"Export failed: {e}") from e


async def stop_all_workers_async() -> int:
    running_count = sum(
        1 for p in st.session_state.running_pipelines.values()
        if p.get("status") == "running"
    )
    st.session_state.running_pipelines = {}
    # Cancel all tracked futures (T1, T2, T3)
    for key, future in list(_PIPELINE_FUTURES.items()):
        future.cancel()
    _PIPELINE_FUTURES.clear()
    # Reset progress tracker
    with _PIPELINE_LOCK:
        _PIPELINE_PROGRESS.clear()
    try:
        await db.execute(
            "UPDATE subdomains SET status = ? WHERE status = ?",
            ("pending", "running")
        )
        await db.commit()
    except Exception as e:
        logger.warning(f"DB update on stop failed: {e}")
    return running_count


async def get_tools_async(subdomain_id: int) -> list[dict]:
    return await get_tools(subdomain_id)


async def get_features_async(subdomain_id: int) -> list[dict]:
    return await get_features(subdomain_id)


def inject_css() -> None:
    """Global CSS: tree-node buttons + micro-animations."""
    # Use both .stButton (stable class) AND data-testid attribute selectors
    # so we beat Emotion-generated class specificity with !important on all levels.
    st.markdown("""
    <style>
    /* ═══════════════════════════════════════════════════════════════
       LEVEL 1 — wrapper div: block layout, full width, left-aligned.
       Streamlit uses BOTH class .stButton and data-testid="stButton".
    ═══════════════════════════════════════════════════════════════ */
    .stButton,
    div[data-testid="stButton"] {
        display:    block !important;
        width:      100% !important;
        text-align: left !important;
    }

    /* ═══════════════════════════════════════════════════════════════
       LEVEL 2 — the <button> element (Only apply to TERTIARY tree buttons).
       Emotion inlines justify-content:center — override.
    ═══════════════════════════════════════════════════════════════ */
    .stButton > button[data-testid*="tertiary"],
    div[data-testid="stButton"] > button[data-testid*="tertiary"] {
        display:          flex !important;
        justify-content:  flex-start !important;
        align-items:      center !important;
        text-align:       left !important;
        width:            100% !important;
        border:           1px solid transparent !important;
        background:       transparent !important;
        padding:          3px 8px !important;
        color:            #cbd5e1 !important;
        font-size:        13px !important;
        font-family:      'JetBrains Mono', 'Fira Code', ui-monospace, monospace !important;
        border-radius:    4px !important;
        line-height:      1.5 !important;
        letter-spacing:   0.01em !important;
        transition:       background 0.1s ease, border-color 0.1s ease !important;
        white-space:      nowrap !important;
        overflow:         hidden !important;
    }

    /* ═══════════════════════════════════════════════════════════════
       LEVEL 3 & 4 — ALL inner children inside the TERTIARY button.
       Streamlit injects inner wrappers (like stMarkdownContainer) that
       shrink-wrap text and center it. We force all children to 100% width.
    ═══════════════════════════════════════════════════════════════ */
    .stButton > button[data-testid*="tertiary"] > div,
    div[data-testid="stButton"] > button[data-testid*="tertiary"] > div {
        display:          flex !important;
        flex-direction:   row !important;
        justify-content:  flex-start !important;
        align-items:      center !important;
        text-align:       left !important;
        width:            100% !important;
        gap:              8px !important;
        margin:           0 !important;
    }
    
    .stButton > button[data-testid*="tertiary"] p,
    div[data-testid="stButton"] > button[data-testid*="tertiary"] p {
        margin:           0 !important;
        text-align:       left !important;
        overflow:         hidden !important;
        text-overflow:    ellipsis !important;
    }

    /* ═══ HOVER / FOCUS / ACTIVE ════════════════════════════════════ */
    .stButton > button[data-testid*="tertiary"]:hover,
    div[data-testid="stButton"] > button[data-testid*="tertiary"]:hover {
        background:   rgba(255,255,255,0.06) !important;
        border-color: rgba(255,255,255,0.10) !important;
    }
    .stButton > button[data-testid*="tertiary"]:focus,
    div[data-testid="stButton"] > button[data-testid*="tertiary"]:focus {
        outline:      none !important;
        box-shadow:   none !important;
        background:   rgba(59,130,246,0.10) !important;
        border-color: rgba(59,130,246,0.30) !important;
    }
    .stButton > button[data-testid*="tertiary"]:active,
    div[data-testid="stButton"] > button[data-testid*="tertiary"]:active {
        background:   rgba(59,130,246,0.18) !important;
        border-color: rgba(59,130,246,0.55) !important;
    }

    /* ═══ PRIMARY buttons — preserve their look ══════════════════════ */
    .stButton > button[data-testid="baseButton-primary"]:hover,
    div[data-testid="stButton"] > button[data-testid="baseButton-primary"]:hover {
        box-shadow: 0 0 0 2px rgba(59,130,246,0.45) !important;
    }

    /* ═══ DISABLED / GHOST buttons ═══════════════════════════════════ */
    .stButton > button[disabled],
    button[disabled] {
        opacity:      0.32 !important;
        cursor:       not-allowed !important;
        font-style:   italic !important;
        background:   transparent !important;
        border-color: transparent !important;
        box-shadow:   none !important;
    }

    /* ═══ COLUMN LAYOUT — kill gaps between columns in tree rows ══════ */
    /* Target only nested column blocks (like inside the tree or detail panel) to avoid breaking the main layout */
    [data-testid="stVerticalBlock"] > [data-testid="stVerticalBlock"] [data-testid="stHorizontalBlock"] {
        gap:         2px !important;
        align-items: center !important;
    }
    [data-testid="stVerticalBlock"] > [data-testid="stVerticalBlock"] [data-testid="stHorizontalBlock"] > div[data-testid="column"] {
        padding-left:  1px !important;
        padding-right: 1px !important;
        min-width:     0 !important;
        /* No visible border/shadow on individual columns */
        border:        none !important;
        box-shadow:    none !important;
    }

    /* ═══ CHECKBOX — inline alignment with tree text ══════════════════ */
    [data-testid="stCheckbox"] {
        margin-top:   2px !important;
        padding:      0 !important;
        min-width:    0 !important;
    }
    [data-testid="stCheckbox"] label {
        min-height:   0 !important;
        gap:          0 !important;
        padding-left: 0 !important;
    }
    /* Hide the checkbox label text (we use label_visibility="collapsed") */
    [data-testid="stCheckbox"] > label > span:last-child {
        display: none !important;
    }

    /* ═══ SCROLLBAR — thin track in tree container ════════════════════ */
    [data-testid="stVerticalBlockBorderWrapper"] > div::-webkit-scrollbar {
        width: 3px;
    }
    [data-testid="stVerticalBlockBorderWrapper"] > div::-webkit-scrollbar-thumb {
        background:    #1e3a5f;
        border-radius: 3px;
    }

    /* ═══ RUNNING PULSE animation ════════════════════════════════════ */
    @keyframes pulse-blue {
        0%, 100% { opacity: 1; }
        50%       { opacity: 0.5; }
    }


    /* ═══ SIDEBAR SETTINGS BOTTOM PIN (STICKY POSITION) ══════════════ */
    [data-testid="stSidebarUserContent"] {
        height: 100vh;
    }
    .st-key-sidebar_bottom {
        position: sticky;
        bottom: 0px;
        z-index: 999;
        background-color: var(--secondary-background-color);
        padding-top: 1rem;
        padding-bottom: 1rem;
        margin-top: 3rem;
        border-top: 1px solid rgba(255, 255, 255, 0.05);
    }

    /* ═══ DISABLE SELECTBOX TYPING ═══════════════════════════════════ */
    div[data-baseweb="select"] input {
        caret-color: transparent !important;
        pointer-events: none !important;
    }

    /* ═══ HIDE DEFAULT STREAMLIT HEADER/MENU ═════════════════════════ */
    header[data-testid="stHeader"] {
        display: none !important;
    }
    </style>
    """, unsafe_allow_html=True)


def render_sidebar() -> str:
    with st.sidebar:
        search_svg = icon_html("search", "actions", 20)
        st.markdown(f"<h3 style='margin-bottom:0;'>{search_svg} Cybersec Research</h3>", unsafe_allow_html=True)
        
        st.markdown("---")
        nav_selection = st.selectbox(
            "Techniques",
            options=[
                "Feature Matrices (T1)",
                "Domain Rankings (T2)",
                "NIST CSF Mapping (T3)",
                "Cross-Domain Analysis (T4)",
                "Strategic Score Card (T5)"
            ],
            index=0,
            label_visibility="collapsed",
            filter_mode=None,
        )
        
        st.markdown("---")
        st.markdown("**Config**")
        st.caption(f"Max Workers: `{settings.max_workers}`")
        st.caption(f"DB: `{settings.db_path}`")
        last_export = st.session_state.get("last_export_path")
        if last_export:
            display_output = last_export
        else:
            if nav_selection == "Domain Rankings (T2)":
                display_output = settings.excel_output_path.replace(".xlsx", "_T2_Rankings.xlsx")
            elif nav_selection == "Cross-Domain Analysis (T4)":
                display_output = settings.t4_excel_output_path
            elif nav_selection == "Strategic Score Card (T5)":
                display_output = settings.t5_excel_output_path
            else:
                display_output = settings.excel_output_path
        st.caption(f"Output: `{display_output}`")
        st.markdown("---")
        st.caption("Use the Explorer tab controls to Refresh, Stop workers, or Export.")
        with st.container(key="sidebar_bottom"):
            if st.button("Read Documentation", key="btn_sidebar_docs", width="stretch", icon=":material/description:"):
                st.session_state.show_docs = True

            if st.button("Settings", key="btn_sidebar_settings", width="stretch", icon=":material/settings:"):
                st.session_state.show_settings = True

        return nav_selection


def render_settings_modal() -> None:
    if not st.session_state.get("show_settings"):
        return

    @st.dialog("System Settings", width="large")
    def show_settings():
        st.markdown("Modify the active configuration. Changes apply immediately and are saved to `.env`.")
        
        settings_dict = settings.model_dump()
        new_values = {}
        
        with st.form("settings_form"):
            for key, value in settings_dict.items():
                if isinstance(value, bool):
                    new_values[key] = st.toggle(key, value=value)
                elif isinstance(value, int):
                    new_values[key] = st.number_input(key, value=value, step=1)
                elif isinstance(value, float):
                    new_values[key] = st.number_input(key, value=value, step=0.1)
                else:
                    new_values[key] = st.text_input(key, value=str(value))
            
            c1, c2 = st.columns(2)
            with c1:
                submitted = st.form_submit_button("Save Settings", type="primary", width="stretch")
            with c2:
                cancel = st.form_submit_button("Cancel", width="stretch")
                
            if submitted:
                # Update in-memory
                for k, v in new_values.items():
                    setattr(settings, k, v)
                
                # Update .env file
                import os
                env_path = ".env"
                lines = []
                if os.path.exists(env_path):
                    with open(env_path, "r") as f:
                        lines = f.readlines()
                
                for k, v in new_values.items():
                    found = False
                    for i, line in enumerate(lines):
                        if line.startswith(f"{k}="):
                            lines[i] = f"{k}={str(v)}\n"
                            found = True
                            break
                    if not found:
                        if lines and not lines[-1].endswith("\n"):
                            lines[-1] += "\n"
                        lines.append(f"{k}={str(v)}\n")
                        
                with open(env_path, "w") as f:
                    f.writelines(lines)
                
                st.session_state.show_settings = False
                st.rerun()
                
            if cancel:
                st.session_state.show_settings = False
                st.rerun()

    show_settings()


def render_mermaid(code: str) -> None:
    import base64
    
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ background: #0e1117; color: #fafafa; font-family: sans-serif; margin: 0; padding: 20px; }}
        </style>
    </head>
    <body>
        <script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
        <script>
            mermaid.initialize({{ 
                startOnLoad: true, 
                theme: 'dark',
                flowchart: {{ useMaxWidth: true }}
            }});
        </script>
        <pre class="mermaid">{code}</pre>
    </body>
    </html>
    """
    
    b64 = base64.b64encode(html_content.encode('utf-8')).decode('utf-8')
    data_url = f"data:text/html;base64,{b64}"
    st.iframe(src=data_url, height=500)


def render_documentation_modal() -> None:
    if not st.session_state.get("show_docs"):
        return

    @st.dialog("Documentation", width="large")
    def show_docs():
        readme_path = Path(__file__).parent.parent / "README.md"
        if not readme_path.exists():
            st.error("README.md not found")
            return
        
        content = readme_path.read_text(encoding="utf-8")
        
        mermaid_pattern = r'```mermaid\n(.*?)```'
        parts = re.split(mermaid_pattern, content, flags=re.DOTALL)
        
        for i, part in enumerate(parts):
            if i % 2 == 1:
                try:
                    render_mermaid(part.strip())
                except Exception:
                    st.code(part, language="mermaid")
            else:
                clean_part = re.sub(r'```\w*\n?', '', part)
                if clean_part.strip():
                    st.markdown(clean_part)
        
        if st.button("Close", key="close_docs_btn"):
            st.session_state.show_docs = False
            st.rerun()

    show_docs()


def render_log_panel() -> None:
    list_svg = icon_html("file", "ui", 16)
    st.markdown(f"<h3 style='margin-bottom:0;'>{list_svg} Logs</h3>", unsafe_allow_html=True)

    log_dir = Path(settings.log_dir).resolve()

    session_logs = sorted(log_dir.glob("session_*.log"), reverse=True)
    main_log = log_dir / settings.log_file

    all_log_options: list[str] = []
    if session_logs:
        all_log_options += [f.name for f in session_logs]
    if main_log.exists():
        all_log_options.append(main_log.name)

    if not all_log_options:
        st.info(
            "No log files found yet. Run a pipeline first - logs will appear here "
            f"once the agent writes to `{log_dir}/`."
        )
        return

    col_select, col_refresh = st.columns([4, 1])
    with col_select:
        selected_log = st.selectbox(
            "Log file",
            options=all_log_options,
            index=0,
            help="session_*.log files contain per-run structured logs; cybersec_agent.log is the rolling combined log.",
        )
    with col_refresh:
        st.markdown("&nbsp;", unsafe_allow_html=True)
        if st.button("Refresh", width="stretch", key="log_refresh", icon=":material/refresh:"):
            st.rerun()

    log_path = (log_dir / selected_log).resolve()

    if not str(log_path).startswith(str(log_dir)):
        st.error("Invalid log file path")
        return

    if not log_path.exists():
        st.warning(f"Log file not found: `{log_path}`")
        return

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        if not lines:
            st.info("Log file is empty.")
            return

        display_lines = settings.log_display_lines
        start_idx = max(0, len(lines) - display_lines)
        content_lines = lines[start_idx:]

        size_kb = log_path.stat().st_size / 1024
        size_str = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb/1024:.1f} MB"
        file_svg = icon_html("file", "ui", 12)
        st.markdown(
            f"<p style='font-size:14px; color:#94a3b8;'>{file_svg} <code>{selected_log}</code> / {len(lines):,} lines / {size_str}  "
            + (f"/ showing last {display_lines}" if len(lines) > display_lines else "/ showing all")
            + "</p>", unsafe_allow_html=True
        )

        numbered_content = "".join(
            f"{i + start_idx + 1:6d} | {line}"
            for i, line in enumerate(content_lines)
        )
        st.code(numbered_content, language="log")

    except Exception as e:
        logger.error(f"Failed to read log: {e}")
        st.error(f"Failed to read log: {e}")



def handle_pending_actions(tree_data: dict[str, list[dict]]) -> None:
    pending = st.session_state.pop("_pending_action", None)

    if not pending:
        return

    action_type = pending[0]

    if action_type == "discover":
        domain = pending[1]
        with st.spinner(f"Discovering subdomains for {domain}..."):
            try:
                result = run_async(discover_subdomains_async(domain))
                st.session_state.status_message = f"Discovered {len(result)} subdomains for {domain}"
                st.session_state.status_type = "success"
            except Exception as e:
                st.error(f"Discovery failed: {e}")

    elif action_type == "run_pipeline":
        # Non-blocking: register the pipeline and start a background thread.
        # The thread has its OWN asyncio event loop and no Streamlit context.
        # Progress is tracked in _PIPELINE_PROGRESS (module-level) and synced
        # into session_state.running_pipelines each render via sync_pipeline_state().
        domain, subdomain_id, subdomain_name = pending[1], pending[2], pending[3]
        pipeline_key = f"{domain}_{subdomain_id}"

        existing = st.session_state.running_pipelines.get(pipeline_key, {})
        if existing.get("status") == "running":
            st.session_state.status_message = f"{subdomain_name} is already running"
            st.session_state.status_type = "warning"
        else:
            # Grab the event_queue NOW (on the Streamlit thread) before we leave context
            event_queue = st.session_state.event_queue
            if not event_queue:
                st.session_state.status_message = "Event queue not initialised — reload the page"
                st.session_state.status_type = "error"
                return

            # Pre-populate module-level state so the UI shows it immediately
            initial = {
                "domain":         domain,
                "subdomain_id":   subdomain_id,
                "subdomain_name": subdomain_name,
                "progress":       0.0,
                "step":           "m2",
                "message":        "Queued...",
                "status":         "queued",
            }
            with _PIPELINE_LOCK:
                _PIPELINE_PROGRESS[pipeline_key] = initial
            st.session_state.running_pipelines[pipeline_key] = dict(initial)

            st.session_state.status_message = (
                f"Pipeline queued: {subdomain_name} - check the Active Pipelines tab for live progress"
            )
            st.session_state.status_type = "info"

            def _run_in_thread(
                _domain=domain,
                _sid=subdomain_id,
                _sname=subdomain_name,
                _queue=event_queue,
                _pkey=pipeline_key
            ):
                with _PIPELINE_LOCK:
                    if _pkey in _PIPELINE_PROGRESS:
                        _PIPELINE_PROGRESS[_pkey]["status"] = "running"
                        _PIPELINE_PROGRESS[_pkey]["message"] = "Starting..."
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(
                        run_pipeline_async(_domain, _sid, _sname, _queue)
                    )
                except Exception as exc:
                    logger.error(f"Background pipeline failed for {_sname}: {exc}")
                finally:
                    loop.close()

            future = _EXECUTOR.submit(_run_in_thread)
            _PIPELINE_FUTURES[pipeline_key] = future

    elif action_type == "export":
        subdomain_id, subdomain_name = pending[1], pending[2]
        try:
            output_path = run_async(export_subdomain_async(subdomain_id, subdomain_name))
            st.session_state.last_export_path = output_path
            st.toast("Successfully exported", icon=":material/check_circle:")
        except Exception as e:
            st.toast(f"Export failed: {e}", icon=":material/error:")

    elif action_type == "export_all":
        try:
            output_path = run_async(export_all_async())
            st.session_state.last_export_path = output_path
            st.toast("Successfully exported", icon=":material/check_circle:")
        except Exception as e:
            st.error(f"Export failed: {e}")

    elif action_type == "stop_pipeline":
        subdomain_id = pending[1]
        pipeline_key = None
        for key, pipeline in st.session_state.running_pipelines.items():
            if pipeline.get("subdomain_id") == subdomain_id:
                pipeline_key = key
                break
        if pipeline_key:
            future = _PIPELINE_FUTURES.get(pipeline_key)
            if future:
                future.cancel()
            with _PIPELINE_LOCK:
                if pipeline_key in _PIPELINE_PROGRESS:
                    _PIPELINE_PROGRESS[pipeline_key]["status"] = "stopped"
            st.session_state.running_pipelines[pipeline_key]["status"] = "stopped"
            st.session_state.status_message = f"Pipeline stopped"
            st.session_state.status_type = "warning"
        else:
            st.session_state.status_message = "Pipeline not found"
            st.session_state.status_type = "error"

    elif action_type == "stop_all":
        count = run_async(stop_all_workers_async())
        st.session_state.status_message = f"Stopped {count} workers"
        st.session_state.status_type = "warning"

    elif action_type == "refresh":
        cleanup_completed_pipelines()
        st.session_state.status_message = "Refreshed"
        st.session_state.status_type = "info"


def render_explorer_header(tree_data: dict) -> None:
    hcol1, hcol_m, hcol2, hcol3, hcol4 = st.columns([2.8, 1.8, 1, 1, 1])

    total_domains = len(tree_data) if tree_data else 0
    all_subdomains = [sd for sublist in tree_data.values() for sd in sublist] if tree_data else []
    total_subdomains = len(all_subdomains)

    with hcol1:
        st.markdown(
            "<p style='font-size:13px; color:#64748b; margin:6px 0 0;'>"
            "Select a domain or subdomain in the tree to view details and actions."
            "</p>",
            unsafe_allow_html=True,
        )
        
    with hcol_m:
        st.markdown(
            f"<div style='display:flex; gap:24px; align-items:center; height:32px; margin-top:2px; justify-content:flex-end; padding-right:12px;'>"
            f"<div><span style='color:#64748b; font-size:13px;'>Domains:</span> <strong style='color:#f8fafc; font-size:15px;'>{total_domains}</strong></div>"
            f"<div><span style='color:#64748b; font-size:13px;'>Subdomains:</span> <strong style='color:#f8fafc; font-size:15px;'>{total_subdomains}</strong></div>"
            f"</div>",
            unsafe_allow_html=True,
        )

    with hcol2:
        if st.button("Refresh", width="stretch", help="Clear completed pipelines and refresh", icon=":material/refresh:"):
            cleanup_completed_pipelines()
            st.session_state.status_message = "Refreshed"
            st.session_state.status_type = "info"
            st.rerun()

    with hcol3:
        if st.button("Stop All", width="stretch", help="Stop all running workers", icon=":material/stop:"):
            count = run_async(stop_all_workers_async())
            st.session_state.status_message = f"Stopped {count} workers"
            st.session_state.status_type = "warning"
            st.rerun()

    with hcol4:
        if st.button("Export All", width="stretch", help="Export all completed subdomains", icon=":material/bar_chart:"):
            try:
                output_path = run_async(export_all_async())
                st.session_state.last_export_path = output_path
                st.toast("Successfully exported", icon=":material/check_circle:")
            except Exception as e:
                st.toast("Export failed", icon=":material/error:")

    st.markdown(
        "<hr style='border-color:#1e2533; margin:8px 0;'>",
        unsafe_allow_html=True,
    )


def main() -> None:
    st.set_page_config(
        page_title="Cybersec Research Agent",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    init_session_state()
    run_async(init_app_async())

    inject_css()
    current_nav = render_sidebar()
    render_documentation_modal()
    render_settings_modal()

    if current_nav == "Feature Matrices (T1)":
        search_svg = icon_html("search", "actions", 32)
        st.markdown(f"<h1 style='margin-bottom:0px; padding-bottom:8px;'>{search_svg} Cybersec Research Agent</h1>", unsafe_allow_html=True)
        st.markdown("Discover, analyze, and compare cybersecurity tools across domains.")

        if st.session_state.status_message:
            msg = st.session_state.status_message
            mtype = st.session_state.status_type
            st.toast(msg)
            st.session_state.status_message = ""
            st.session_state.status_type = "info"

        with st.spinner("Loading domains..."):
            tree_data = run_async(load_domain_tree_async())

        sync_pipeline_state()

        handle_pending_actions(tree_data)

        tab1, tab2, tab3 = st.tabs([":material/folder: Explorer", ":material/play_arrow: Active Pipelines", ":material/description: Logs"])

        # ── Explorer tab ───────────────────────────────────────────────────────────
        with tab1:
            render_explorer_header(tree_data)

            # Use a standard 2-column layout with a smaller gap
            col_tree, col_detail = st.columns([1, 1.8], gap="medium")

            with col_tree:
                st.markdown(
                    "<p style='font-size:11px; font-weight:700; letter-spacing:1px;"
                    "color:#475569; text-transform:uppercase; margin-bottom:6px;"
                    "text-align:left;'>Domains</p>",
                    unsafe_allow_html=True,
                )
                render_domain_tree(tree_data)

            with col_detail:
                st.markdown(
                    "<p style='font-size:11px; font-weight:700; letter-spacing:1px;"
                    "color:#475569; text-transform:uppercase; margin-bottom:6px;"
                    "text-align:left; padding-left:4px;'>Details &amp; Actions</p>",
                    unsafe_allow_html=True,
                )
                render_detail_panel(
                    tree_data,
                    pipeline_info=st.session_state.running_pipelines,
                    on_discover=lambda domain: st.session_state.update(
                        _pending_action=("discover", domain)
                    ) or st.rerun(),
                    on_run=lambda domain, sid, sname: st.session_state.update(
                        _pending_action=("run_pipeline", domain, sid, sname)
                    ) or st.rerun(),
                    on_export=lambda sid, sname: st.session_state.update(
                        _pending_action=("export", sid, sname)
                    ) or st.rerun(),
                    on_stop=lambda sid: st.session_state.update(
                        _pending_action=("stop_pipeline", sid)
                    ) or st.rerun(),
                    on_run_all=lambda domain, subs: None,
                    on_export_all=lambda domain: st.session_state.update(
                        _pending_action=("export_all",)
                    ) or st.rerun(),
                )

            # Bulk selection bar (contextual — only expands when items are selected)
            selection_count = get_selection_count()
            render_bulk_action_bar(selection_count)

            # Handle bulk actions triggered from the bar
            bulk_action = st.session_state.pop("_bulk_action", None)
            if bulk_action == "bulk_run":
                selected_domains: set[str] = set()
                for item in st.session_state.get("selected_items", set()):
                    domain = item.rsplit("_", 1)[0] if "_" in item else item
                    selected_domains.add(domain)
                for domain in selected_domains:
                    st.session_state._pending_action = ("discover", domain)
                st.rerun()
            elif bulk_action == "bulk_export":
                st.session_state._pending_action = ("export_all",)
                st.rerun()
            elif bulk_action == "bulk_clear":
                st.session_state.selected_items = set()
                st.rerun()

        # ── Active Pipelines tab ───────────────────────────────────────────────────
        with tab2:
            # Sync again so this tab reflects the latest background progress
            sync_pipeline_state()

            # Auto-refresh toggle
            auto_refresh = st.toggle("Auto-refresh (every 3s)", value=False, key="auto_refresh")

            running_pipelines = {
                k: v for k, v in st.session_state.running_pipelines.items()
                if not k.startswith("t2_") and not k.startswith("t3_") and not k.startswith("t4_")
            }

            if not running_pipelines:
                st.info("No pipelines are currently tracked. Start one from the Explorer tab.")
            else:
                for key, pipeline in list(running_pipelines.items()):
                    status       = pipeline.get("status", "pending")
                    sname        = pipeline.get("subdomain_name", key)
                    domain       = pipeline.get("domain", "")
                    progress     = pipeline.get("progress", 0.0)
                    step         = pipeline.get("step", "d1").upper()
                    message      = pipeline.get("message", "")

                    status_cfg = {
                        "queued": (status_icon("pending", 14), "#94a3b8"),
                        "running": (status_icon("running", 14), "#3b82f6"),
                        "done": (status_icon("done", 14), "#22c55e"),
                        "failed": (status_icon("failed", 14), "#ef4444"),
                    }.get(status, (status_icon("pending", 14), "#6b7280"))
                    s_icon, s_color = status_cfg

                    STAGE_LABELS = {
                        "d1": "Tool Ranking",
                    }
                    step_label = STAGE_LABELS.get(step.lower(), step)

                    with st.container(border=True):
                        h1, h2 = st.columns([4, 1])
                        with h1:
                            st.markdown(
                                f"<span style='font-size:15px; font-weight:700;'>{sname}</span>&ensp;"
                                f"<span style='font-size:12px; color:#64748b;'>{domain}</span>",
                                unsafe_allow_html=True,
                            )
                        with h2:
                            st.markdown(
                                f"<span style='color:{s_color}; font-weight:600;'>{s_icon} {status.capitalize()}</span>",
                                unsafe_allow_html=True,
                            )

                        if status in ("running", "queued"):
                            st.progress(progress, text=f"[{step}] {step_label}" if status == "running" else "Waiting in queue...")
                            if message:
                                msg_svg = icon_html("message", "ui", 12)
                                st.markdown(
                                    f"<p style='font-size:12px; color:#94a3b8; margin:2px 0 6px;'>"
                                    f"{msg_svg} {message}</p>",
                                    unsafe_allow_html=True,
                                )
                            stages = [
                                ("M2", "Tools", progress > 0.25),
                                ("M3", "Features", progress > 0.50),
                                ("M4", "Subfeatures", progress > 0.70),
                                ("M5", "Matrix", progress >= 1.0),
                            ]
                            sc = st.columns(4)
                            for i, (sl, slabel, done) in enumerate(stages):
                                with sc[i]:
                                    icon = icon_html("check", "ui", 12) if done else status_icon("running" if step.lower() == sl.lower() else "pending", 12)
                                    st.markdown(
                                        f"<div style='text-align:center; font-size:11px; color:#64748b;'>"
                                        f"{icon} <strong>{sl}</strong><br/>{slabel}</div>",
                                        unsafe_allow_html=True,
                                    )

                            log_dir = Path(settings.log_dir)
                            log_files = sorted(log_dir.glob("session_*.log"), reverse=True)
                            if log_files:
                                with st.expander("Recent log lines", expanded=False, icon=":material/description:"):
                                    try:
                                        with open(log_files[0], "r", encoding="utf-8", errors="replace") as f:
                                            tail = f.readlines()[-50:]
                                        st.code("".join(tail), language="log")
                                    except Exception:
                                        st.caption("Could not read log file.")

                        elif status == "failed":
                            st.error(f"Pipeline failed: {message or 'Unknown error'}")
                        elif status == "done":
                            st.success(f"Completed — {int(progress * 100)}%")

            st.markdown("")
            cc1, cc2 = st.columns(2)
            with cc1:
                if st.button("Clear Completed / Failed", width="stretch", icon=":material/delete:"):
                    cleanup_completed_pipelines()
                    st.rerun()
            with cc2:
                if st.button("Refresh Now", width="stretch", icon=":material/refresh:"):
                    sync_pipeline_state()
                    st.rerun()

            if auto_refresh:
                from streamlit_autorefresh import st_autorefresh
                st_autorefresh(interval=3000, key="ar_t1")

        # ── Logs tab ───────────────────────────────────────────────────────────────
        with tab3:
            render_log_panel()

    elif current_nav == "Domain Rankings (T2)":
        trophies_svg = icon_html("emoji_events", "actions", 32)
        st.markdown(f"<h1 style='margin-bottom:0px; padding-bottom:8px;'>{trophies_svg} Subdomain Tool Rankings</h1>", unsafe_allow_html=True)
        st.markdown("Rank tools within each subdomain based on feature coverage and market presence.")
        
        if st.session_state.status_message:
            msg = st.session_state.status_message
            st.toast(msg)
            st.session_state.status_message = ""
        
        from db.subdomain_store import get_all_t2_subdomain_rankings, get_t2_subdomain_tools, get_t2_subdomain_ranking, get_eligible_subdomains
        
        with st.spinner("Loading eligible subdomains..."):
            eligible = run_async(get_eligible_subdomains())
            t2_rankings = run_async(get_all_t2_subdomain_rankings())
        
        sync_pipeline_state()
        
        pending_action = st.session_state.pop("_pending_t2_action", None)
        
        if pending_action:
            action_type = pending_action[0]
            
            if action_type == "run_subdomain":
                subdomain_id, subdomain_name = pending_action[1], pending_action[2]
                st.session_state._pending_t2_pipeline = (subdomain_id, subdomain_name)
                st.rerun()
            elif action_type == "export_t2":
                subdomain_id, subdomain_name = pending_action[1], pending_action[2]
                try:
                    output_path = run_async(export_t2_subdomain_async(subdomain_id, subdomain_name))
                    st.session_state.last_export_path = output_path
                    st.toast("Successfully exported", icon=":material/check_circle:")
                except Exception as e:
                    st.toast(f"Export failed: {e}", icon=":material/error:")
            elif action_type == "export_all_t2":
                try:
                    output_path = run_async(export_all_t2_async())
                    st.session_state.last_export_path = output_path
                    st.toast("Successfully exported", icon=":material/check_circle:")
                except Exception as e:
                    st.toast(f"Export failed: {e}", icon=":material/error:")
        
        tab1, tab2, tab3 = st.tabs([":material/folder: Explorer", ":material/play_arrow: Active Pipelines", ":material/description: Logs"])
        
        with tab1:
            hcol1, hcol2, hcol3, hcol4 = st.columns([2, 1, 1, 1])
            
            with hcol1:
                st.markdown(
                    "<p style='font-size:13px; color:#64748b; margin:6px 0 0;'>"
                    f"{len(eligible)} subdomains have tools ready for ranking."
                    "</p>",
                    unsafe_allow_html=True,
                )
            
            with hcol2:
                auto_refresh_t2 = st.toggle("Auto (3s)", value=False, key="auto_refresh_t2")
            
            with hcol3:
                if st.button("Refresh", width="stretch", icon=":material/refresh:"):
                    st.rerun()
                    
            with hcol4:
                if st.button("Export All", width="stretch", icon=":material/download:"):
                    st.session_state._pending_t2_action = ("export_all_t2",)
                    st.rerun()
            
            st.markdown("<hr style='border-color:#1e2533; margin:8px 0;'>", unsafe_allow_html=True)
            
            col_tree, col_detail = st.columns([1, 1.8], gap="medium")
            
            with col_tree:
                st.markdown(
                    "<p style='font-size:11px; font-weight:700; letter-spacing:1px;"
                    "color:#475569; text-transform:uppercase; margin-bottom:6px;"
                    "text-align:left;'>Subdomains with Tools</p>",
                    unsafe_allow_html=True,
                )
                
                with st.container(height=750, border=False):
                    t2_by_subdomain = {r["subdomain_id"]: r for r in t2_rankings}
                    
                    current_domain = None
                    for sd in eligible:
                        domain_name = sd.get("domain_name", "")
                        subdomain_name = sd.get("name", "")
                        subdomain_id = sd.get("id")
                        tool_count = sd.get("tool_count", 0)
                        
                        if domain_name != current_domain:
                            st.markdown(f"**{domain_name}**")
                            current_domain = domain_name
                        
                        ranking = t2_by_subdomain.get(subdomain_id)
                        db_status = ranking.get("status", "pending") if ranking else "pending"
                        
                        pipeline_key = f"t2_{subdomain_name}"
                        running_info = st.session_state.running_pipelines.get(pipeline_key, {})
                        if running_info.get("status") == "running":
                            t2_status = "running"
                        else:
                            t2_status = db_status
                        
                        col1, col2, col3 = st.columns([3, 1, 1])
                        
                        with col1:
                            btn_icon = ":material/sync:" if t2_status == "running" else None
                            if st.button(subdomain_name, key=f"t2_sd_{subdomain_id}", width="stretch", icon=btn_icon):
                                st.session_state.t2_selected_subdomain = subdomain_id
                                st.session_state.t2_selected_subdomain_name = subdomain_name
                        
                        with col2:
                            status_icon_svg = status_icon(t2_status, 14)
                            st.markdown(f"<span style='font-size:14px;'>{status_icon_svg}</span>", unsafe_allow_html=True)
                        
                        with col3:
                            if t2_status == "running":
                                st.button("...", key=f"t2_run_btn_{subdomain_id}", disabled=True, width="stretch")
                            elif t2_status == "done":
                                if st.button("Rerun", key=f"t2_rerun_{subdomain_id}", width="stretch"):
                                    st.session_state._pending_t2_action = ("run_subdomain", subdomain_id, subdomain_name)
                                    st.rerun()
                            else:
                                if st.button("Run", key=f"t2_run_{subdomain_id}", width="stretch"):
                                    st.session_state._pending_t2_action = ("run_subdomain", subdomain_id, subdomain_name)
                                    st.rerun()
            
            with col_detail:
                st.markdown(
                    "<p style='font-size:11px; font-weight:700; letter-spacing:1px;"
                    "color:#475569; text-transform:uppercase; margin-bottom:6px;"
                    "text-align:left; padding-left:4px;'>Ranked Tools</p>",
                    unsafe_allow_html=True,
                )
                
                selected_id = st.session_state.get("t2_selected_subdomain")
                selected_name = st.session_state.get("t2_selected_subdomain_name")
                
                if selected_id:
                    ranking = run_async(get_t2_subdomain_ranking(selected_id))
                    tools = run_async(get_t2_subdomain_tools(selected_id))
                    
                    hdr_col1, hdr_col2 = st.columns([3, 1])
                    with hdr_col1:
                        st.markdown(f"**{selected_name}**")
                    with hdr_col2:
                        if tools:
                            if st.button("Export", key=f"t2_exp_{selected_id}", icon=":material/download:", width="stretch"):
                                st.session_state._pending_t2_action = ("export_t2", selected_id, selected_name)
                                st.rerun()
                    
                    if ranking:
                        r_status = ranking.get("status", "pending")
                        
                        pipeline_key = f"t2_{selected_name}"
                        running_info = st.session_state.running_pipelines.get(pipeline_key, {})
                        if running_info.get("status") == "running":
                            r_status = "running"
                        
                        status_icon_svg = status_icon(r_status, 16)
                        status_color = {"done": "#22c55e", "running": "#3b82f6", "failed": "#ef4444"}.get(r_status, "#6b7280")
                        st.markdown(
                            f"<p style='font-size:13px;'>Status: <span style='color:{status_color}; font-weight:600;'>{status_icon_svg} {r_status.capitalize()}</span></p>",
                            unsafe_allow_html=True
                        )
                    
                    if tools:
                        import pandas as pd
                        df_data = []
                        for i, t in enumerate(sorted(tools, key=lambda x: x.get("composite_score", 0), reverse=True)):
                            score = t.get("composite_score", 0)
                            df_data.append({
                                "#": i + 1,
                                "Tool": f"{t.get('vendor', '')} {t.get('product_name', '')}",
                                "Type": t.get("tool_type", "").capitalize(),
                                "Score": f"{score:.1f}",
                            })
                        
                        if df_data:
                            df = pd.DataFrame(df_data)
                            st.dataframe(
                                df,
                                width="stretch",
                                hide_index=True,
                                height=35 * len(df_data) + 40,
                            )
                else:
                    st.info("Select a subdomain from the list to view ranked tools.")
            
            if auto_refresh_t2:
                from streamlit_autorefresh import st_autorefresh
                st_autorefresh(interval=3000, key="ar_t2")
        
        with tab2:
            sync_pipeline_state()
            
            running_pipelines = {
                k: v for k, v in st.session_state.running_pipelines.items()
                if k.startswith("t2_")
            }
            
            if not running_pipelines:
                st.info("No Technique 2 pipelines are currently running. Start one from the Explorer tab.")
            else:
                for key, pipeline in list(running_pipelines.items()):
                    status = pipeline.get("status", "pending")
                    subdomain = pipeline.get("subdomain", key)
                    progress = pipeline.get("progress", 0.0)
                    step = pipeline.get("step", "d1").upper()
                    message = pipeline.get("message", "")
                    
                    status_cfg = {
                        "running": (status_icon("running", 14), "#3b82f6"),
                        "done": (status_icon("done", 14), "#22c55e"),
                        "failed": (status_icon("failed", 14), "#ef4444"),
                    }.get(status, (status_icon("pending", 14), "#6b7280"))
                    s_icon, s_color = status_cfg
                    
                    with st.container(border=True):
                        h1, h2 = st.columns([4, 1])
                        with h1:
                            st.markdown(f"**{subdomain}**", unsafe_allow_html=True)
                        with h2:
                            st.markdown(f"{s_icon} {status.capitalize()}", unsafe_allow_html=True)
                        
                        if status in ("running", "queued"):
                            st.progress(progress, text=f"[{step}] {message}" if status == "running" else "Waiting in queue...")
        
        with tab3:
            render_log_panel()

    elif current_nav == "NIST CSF Mapping (T3)":
        # ── T3 header ─────────────────────────────────────────────────────────
        grid_svg = icon_html("grid_view", "actions", 32)
        st.markdown(f"<h1 style='margin-bottom:0px; padding-bottom:8px;'>{grid_svg} Tool Classification Matrix</h1>", unsafe_allow_html=True)
        st.markdown("Cross-domain classification of all tools with NIST CSF 2.0 function mapping.")

        if st.session_state.status_message:
            st.toast(st.session_state.status_message)
            st.session_state.status_message = ""

        sync_pipeline_state()

        from db.t3_store import (
            get_t3_tools_with_coverage, get_t3_tool_memberships,
            get_t3_stats, get_t3_run_status, get_t3_domain_list,
        )

        t3_run = run_async(get_t3_run_status())
        t3_stats = run_async(get_t3_stats())

        tab1, tab2, tab3, tab4 = st.tabs([
            ":material/folder: Explorer",
            ":material/bar_chart: Metrics & Insights",
            ":material/play_arrow: Active Pipelines",
            ":material/description: Logs",
        ])

        # ── Explorer tab ──────────────────────────────────────────────────────
        with tab1:
            # ── Action bar ──
            hc1, hc2, hc3, hc4, hc5 = st.columns([2, 1, 1, 1, 1])
            with hc1:
                db_status = (t3_run or {}).get("status", "never")
                t3_pipeline = st.session_state.running_pipelines.get("t3_classification")
                if t3_pipeline and t3_pipeline.get("status") in ("queued", "running"):
                    run_status = "running"
                else:
                    run_status = db_status
                
                total_t3 = t3_stats.get("total", 0)
                st.markdown(
                    f"<p style='font-size:13px; color:#64748b; margin:6px 0 0;'>"
                    f"{total_t3} unique tools classified &nbsp;·&nbsp; status: <strong style='color:#f8fafc'>{run_status}</strong></p>",
                    unsafe_allow_html=True,
                )
            with hc2:
                auto_refresh_t3 = st.toggle("Auto (3s)", value=False, key="auto_refresh_t3")
            with hc3:
                if st.button("Run", width="stretch", icon=":material/play_arrow:", help="Run T3 classification"):
                    st.session_state._pending_t3_pipeline = (False,)
                    st.rerun()
            with hc4:
                if st.button("Re-run", width="stretch", icon=":material/refresh:", help="Wipe and re-classify"):
                    st.session_state._pending_t3_pipeline = (True,)
                    st.rerun()
            with hc5:
                if st.button("Export", width="stretch", icon=":material/download:"):
                    try:
                        path = run_async(export_t3_async())
                        st.session_state.last_export_path = path
                        st.toast("Exported successfully", icon=":material/check_circle:")
                    except Exception as e:
                        st.toast(f"Export failed: {e}", icon=":material/error:")

            st.markdown("<hr style='border-color:#1e2533; margin:8px 0;'>", unsafe_allow_html=True)

            # ── Stats row ──
            s1, s2, s3, s4, s5, s6 = st.columns(6)
            nist_counts = t3_stats.get("nist_counts", {})
            nist_labels = {"ID": "Identify", "PR": "Protect", "DE": "Detect", "RS": "Respond", "RC": "Recover", "GV": "Govern"}
            nist_colors = {"ID": "#1D4ED8", "PR": "#15803D", "DE": "#C2410C", "RS": "#B91C1C", "RC": "#7E22CE", "GV": "#0E7490"}
            for col, func in zip([s1, s2, s3, s4, s5, s6], ["ID", "PR", "DE", "RS", "RC", "GV"]):
                with col:
                    cnt = nist_counts.get(func, 0)
                    color = nist_colors[func]
                    st.markdown(
                        f"<div style='text-align:center; padding:8px; border-radius:6px; background:{color}22; border:1px solid {color}55;'>"
                        f"<div style='font-size:11px; color:#94a3b8; letter-spacing:1px;'>{func} · {nist_labels[func]}</div>"
                        f"<div style='font-size:22px; font-weight:700; color:#f8fafc;'>{cnt}</div>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )

            st.markdown("")

            # ── Filters ──
            fc1, fc2, fc3 = st.columns(3)
            domains_for_filter = run_async(get_t3_domain_list())
            domain_options = {d["name"]: d["id"] for d in domains_for_filter}

            with fc1:
                sel_domain = st.selectbox("Filter by Domain", ["All"] + list(domain_options.keys()),
                                          key="t3_domain_filter", label_visibility="visible")
            with fc2:
                sel_nist = st.selectbox("Filter by NIST Function", ["All"] + list(nist_labels.keys()),
                                        key="t3_nist_filter", format_func=lambda x: x if x == "All" else f"{x} — {nist_labels.get(x,'')}")
            with fc3:
                sel_type = st.selectbox("Filter by Type", ["All", "enterprise", "opensource"],
                                        key="t3_type_filter")

            filter_domain_id = domain_options.get(sel_domain) if sel_domain != "All" else None
            filter_nist = sel_nist if sel_nist != "All" else None
            filter_type = sel_type if sel_type != "All" else None

            # ── Main table + detail panel ──
            col_table, col_detail = st.columns([1.5, 1], gap="medium")

            with col_table:
                with st.spinner("Loading tools..."):
                    t3_tools = run_async(get_t3_tools_with_coverage(
                        filter_domain_id=filter_domain_id,
                        filter_nist=filter_nist,
                        filter_type=filter_type,
                    ))

                if not t3_tools:
                    if total_t3 == 0:
                        st.info("No tools classified yet. Click **Run** to start T3 classification.")
                    else:
                        st.info("No tools match the current filters.")
                else:
                    import json as _json
                    import pandas as pd
                    rows = []
                    for t in t3_tools:
                        nf = t.get("nist_functions") or "[]"
                        try:
                            nf_list = _json.loads(nf) if isinstance(nf, str) else nf
                        except Exception:
                            nf_list = []
                        rows.append({
                            "ID": t["id"],
                            "Vendor": t.get("vendor", ""),
                            "Product": t.get("product_name", ""),
                            "Type": (t.get("tool_type") or "").capitalize(),
                            "NIST Primary": t.get("nist_primary_function") or "–",
                            "NIST Functions": ", ".join(nf_list),
                            "Domains": t.get("domain_count", 0),
                            "Subdomains": t.get("subdomain_count", 0),
                        })
                    df = pd.DataFrame(rows)
                    event = st.dataframe(
                        df.drop(columns=["ID"]),
                        width="stretch",
                        hide_index=True,
                        height=min(35 * len(rows) + 40, 600),
                        on_select="rerun",
                        selection_mode="single-row",
                        key="t3_table",
                    )
                    sel_rows = event.selection.get("rows", []) if event and event.selection else []
                    if sel_rows and 0 <= sel_rows[0] < len(rows):
                        st.session_state.t3_selected_tool_id = rows[sel_rows[0]]["ID"]

            with col_detail:
                sel_id = st.session_state.get("t3_selected_tool_id")
                if sel_id:
                    memberships = run_async(get_t3_tool_memberships(sel_id))
                    sel_tool = next((t for t in t3_tools if t["id"] == sel_id), None)
                    if sel_tool:
                        nf = sel_tool.get("nist_functions") or "[]"
                        try:
                            nf_list = _json.loads(nf) if isinstance(nf, str) else nf
                        except Exception:
                            nf_list = []
                        st.markdown(f"**{sel_tool.get('vendor','')} — {sel_tool.get('product_name','')}**")
                        st.caption(f"Type: {(sel_tool.get('tool_type') or '').capitalize()}")

                        st.markdown("**NIST Functions:**")
                        cols_n = st.columns(len(nf_list) if nf_list else 1)
                        for ci, func in enumerate(nf_list):
                            cols_n[ci].markdown(
                                f"<span style='background:{nist_colors.get(func,'#333')}; color:white; "
                                f"padding:2px 8px; border-radius:4px; font-size:12px; font-weight:700;'>"
                                f"{func} {nist_labels.get(func,'')}</span>",
                                unsafe_allow_html=True,
                            )

                        st.markdown("")
                        st.markdown(f"**Domain Coverage ({sel_tool.get('domain_count', 0)}):**")
                        domains_m = sorted({m["domain_name"] for m in memberships})
                        for d in domains_m:
                            st.markdown(f"- {d}")

                        st.markdown(f"**Subdomain Coverage ({sel_tool.get('subdomain_count', 0)}):**")
                        with st.container(height=200, border=False):
                            prev_dom = None
                            for m in memberships:
                                if m["domain_name"] != prev_dom:
                                    st.caption(m["domain_name"])
                                    prev_dom = m["domain_name"]
                                st.markdown(f"  ↳ {m['subdomain_name']}")
                else:
                    st.info("Select a row from the table to view full domain and subdomain coverage.")

            if auto_refresh_t3:
                from streamlit_autorefresh import st_autorefresh
                st_autorefresh(interval=3000, key="ar_t3")

        # ── Metrics & Insights tab (T3) ───────────────────────────────────────
        with tab2:
            st.markdown("### Global Classification Metrics")
            
            exec_summary = (t3_run or {}).get("executive_summary")
            if exec_summary:
                st.markdown("#### Agentic Executive Summary")
                st.info(exec_summary, icon=":material/psychiatry:")
                st.markdown("<br/>", unsafe_allow_html=True)

            if not t3_tools:
                st.info("No tools classified yet.")
            else:
                import pandas as pd
                import altair as alt
                import json as _json
                import plotly.graph_objects as go

                metrics_data = []
                for t in t3_tools:
                    nf = t.get("nist_functions") or "[]"
                    try:
                        nf_list = _json.loads(nf) if isinstance(nf, str) else nf
                    except Exception:
                        nf_list = []
                    
                    metrics_data.append({
                        "Vendor": t.get("vendor", "Unknown"),
                        "Primary Function": t.get("nist_primary_function") or "Unknown",
                        "Tool Type": (t.get("tool_type") or "Unknown").capitalize(),
                        "Subdomain Count": t.get("subdomain_count", 0),
                        "Domain Count": t.get("domain_count", 0)
                    })
                m_df = pd.DataFrame(metrics_data)

                # Row 1
                mc1, mc2 = st.columns(2)
                
                with mc1:
                    st.markdown("**NIST Primary Function Radar**")
                    if "Primary Function" in m_df.columns and not m_df.empty:
                        func_counts = m_df["Primary Function"].value_counts()
                        categories = ["Identify (ID)", "Protect (PR)", "Detect (DE)", "Respond (RS)", "Recover (RC)", "Govern (GV)"]
                        r_values = []
                        for cat in ["ID", "PR", "DE", "RS", "RC", "GV"]:
                            r_values.append(func_counts.get(cat, 0))
                        
                        fig = go.Figure()
                        fig.add_trace(go.Scatterpolar(
                            r=r_values + [r_values[0]],
                            theta=categories + [categories[0]],
                            fill='toself',
                            name='Tools',
                            line=dict(color="#3b82f6")
                        ))
                        fig.update_layout(
                            polar=dict(
                                radialaxis=dict(visible=True, range=[0, max(r_values) * 1.2 if r_values and max(r_values) > 0 else 10])
                            ),
                            showlegend=False,
                            height=320,
                            margin=dict(t=20, b=20, l=20, r=20),
                            paper_bgcolor="rgba(0,0,0,0)",
                            plot_bgcolor="rgba(0,0,0,0)"
                        )
                        st.plotly_chart(fig, width="stretch")
                
                with mc2:
                    st.markdown("**Tool Type Breakdown**")
                    if "Tool Type" in m_df.columns and not m_df.empty:
                        type_counts = m_df["Tool Type"].value_counts().reset_index()
                        type_counts.columns = ["Type", "Count"]
                        pie = alt.Chart(type_counts).mark_arc(outerRadius=120).encode(
                            theta=alt.Theta(field="Count", type="quantitative"),
                            color=alt.Color(field="Type", type="nominal"),
                            tooltip=["Type", "Count"]
                        ).properties(height=320)
                        st.altair_chart(pie, width="stretch")
                
                st.markdown("<hr style='border-color:#1e2533; margin:16px 0;'>", unsafe_allow_html=True)
                
                # Row 2 (Legacy Charts - Centered)
                _, mc_center, _ = st.columns([1, 2, 1])
                
                with mc_center:
                    st.markdown("<div style='text-align: center; margin-bottom: 8px;'><strong>NIST Primary Function Distribution (Donut)</strong></div>", unsafe_allow_html=True)
                    if "Primary Function" in m_df.columns and not m_df.empty:
                        func_counts_d = m_df["Primary Function"].value_counts().reset_index()
                        func_counts_d.columns = ["Function", "Count"]
                        donut = alt.Chart(func_counts_d).mark_arc(innerRadius=50).encode(
                            theta=alt.Theta(field="Count", type="quantitative"),
                            color=alt.Color(field="Function", type="nominal"),
                            tooltip=["Function", "Count"]
                        ).properties(height=320)
                        st.altair_chart(donut, width="stretch")

                st.markdown("<hr style='border-color:#1e2533; margin:16px 0;'>", unsafe_allow_html=True)
                
                # Row 3
                mc3, mc4 = st.columns(2)
                
                with mc3:
                    st.markdown("**Top 10 Vendors by Footprint**")
                    if "Vendor" in m_df.columns and not m_df.empty:
                        vendor_counts = m_df["Vendor"].value_counts().head(10).reset_index()
                        vendor_counts.columns = ["Vendor", "Count"]
                        bar = alt.Chart(vendor_counts).mark_bar(color="#3b82f6").encode(
                            x=alt.X('Count:Q', title="Number of Tools"),
                            y=alt.Y('Vendor:N', sort='-x', title=""),
                            tooltip=["Vendor", "Count"]
                        ).properties(height=320)
                        st.altair_chart(bar, width="stretch")
                
                with mc4:
                    st.markdown("**Platform vs. Point Solution (Subdomain Coverage)**")
                    if "Subdomain Count" in m_df.columns and not m_df.empty:
                        hist = alt.Chart(m_df).mark_bar(color="#10b981").encode(
                            x=alt.X("Subdomain Count:O", title="Number of Subdomains Covered", axis=alt.Axis(labelAngle=0)),
                            y=alt.Y('count():Q', title="Number of Tools"),
                            tooltip=[alt.Tooltip("Subdomain Count:O", title="Subdomains"), alt.Tooltip("count():Q", title="Tools")]
                        ).properties(height=320)
                        st.altair_chart(hist, width="stretch")

        # ── Active Pipelines tab (T3) ─────────────────────────────────────────
        with tab3:
            sync_pipeline_state()
            t3_pipeline = st.session_state.running_pipelines.get("t3_classification")
            if not t3_pipeline:
                st.info("No T3 classification is running. Click **Run** in the Explorer tab.")
            else:
                status   = t3_pipeline.get("status", "queued")
                progress = t3_pipeline.get("progress", 0.0)
                step     = t3_pipeline.get("step", "s1").upper()
                message  = t3_pipeline.get("message", "")
                s_color  = {"running": "#3b82f6", "done": "#22c55e", "failed": "#ef4444"}.get(status, "#94a3b8")

                with st.container(border=True):
                    h1, h2 = st.columns([4, 1])
                    with h1:
                        st.markdown("**T3 Cross-Domain Classification**")
                    with h2:
                        st.markdown(f"<span style='color:{s_color}; font-weight:600;'>{status.capitalize()}</span>", unsafe_allow_html=True)

                    if status in ("running", "queued"):
                        st.progress(progress, text=f"[{step}] {message}" if status == "running" else "Waiting in queue...")
                        stages = [("S1", "Dedup", progress > 0.25), ("S2", "NIST LLM", progress > 0.95)]
                        sc = st.columns(2)
                        for i, (sl, slabel, done) in enumerate(stages):
                            sc[i].markdown(
                                f"<div style='text-align:center; font-size:11px; color:#64748b;'>"
                                f"{'✔' if done else '○'} <strong>{sl}</strong><br/>{slabel}</div>",
                                unsafe_allow_html=True,
                            )
                    elif status == "failed":
                        st.error(f"Classification failed: {message or 'Unknown error'}")
                    elif status == "done":
                        st.success(f"Complete — {int(progress * 100)}%")

            cc1, cc2, cc3 = st.columns(3)
            with cc1:
                t3_is_active = (t3_pipeline or {}).get("status") in ("running", "queued")
                if st.button(
                    "Stop",
                    width="stretch",
                    icon=":material/stop:",
                    key="t3_stop_btn",
                    disabled=not t3_is_active,
                    type="primary" if t3_is_active else "secondary",
                ):
                    # Cancel the background future
                    future = _PIPELINE_FUTURES.pop("t3_classification", None)
                    if future:
                        future.cancel()
                    with _PIPELINE_LOCK:
                        if "t3_classification" in _PIPELINE_PROGRESS:
                            _PIPELINE_PROGRESS["t3_classification"]["status"] = "failed"
                            _PIPELINE_PROGRESS["t3_classification"]["message"] = "Stopped by user"
                    # Mark failed in DB too
                    try:
                        run_async(__import__("db.t3_store", fromlist=["upsert_t3_run_status"]).upsert_t3_run_status("failed"))
                    except Exception:
                        pass
                    st.toast("T3 classification stopped", icon=":material/stop:")
                    st.rerun()
            with cc2:
                if st.button("Reset Data", width="stretch", icon=":material/delete_forever:", key="t3_reset_btn"):
                    try:
                        run_async(__import__("db.t3_store", fromlist=["reset_t3_data"]).reset_t3_data())
                        st.toast("T3 data reset successfully", icon=":material/check_circle:")
                    except Exception as e:
                        st.toast(f"Reset failed: {e}", icon=":material/error:")
                    st.rerun()
            with cc3:
                if st.button("Refresh", width="stretch", icon=":material/refresh:", key="t3_refresh_btn"):
                    sync_pipeline_state()
                    st.rerun()


        # ── Logs tab ─────────────────────────────────────────────────────────
        with tab4:
            render_log_panel()

    elif current_nav == "Cross-Domain Analysis (T4)":
        analysis_svg = icon_html("analytics", "actions", 32)
        st.markdown(f"<h1 style='margin-bottom:0px; padding-bottom:8px;'>{analysis_svg} Tool-Level Analysis</h1>", unsafe_allow_html=True)
        st.markdown("Analyze cross-domain tools with license detection, feature support, and coverage metrics.")
        
        if st.session_state.status_message:
            st.toast(st.session_state.status_message)
            st.session_state.status_message = ""
        
        from db.t4_store import get_t4_tools_with_coverage, get_t4_stats, get_t4_tool_subdomain_features_list, get_t4_run_status
        
        with st.spinner("Loading tools..."):
            t4_tools = run_async(get_t4_tools_with_coverage())
            t4_stats = run_async(get_t4_stats())
            t4_run = run_async(get_t4_run_status())
        
        sync_pipeline_state()
        
        pending_t4 = st.session_state.pop("_pending_t4_action", None)
        if pending_t4:
            action_type = pending_t4[0]
            if action_type == "export_t4":
                try:
                    from excel.t4_bridge import export_t4_workbook
                    output_path = run_async(export_t4_workbook())
                    st.session_state.last_export_path = output_path
                    st.toast("Excel exported successfully", icon=":material/check_circle:")
                except Exception as e:
                    st.toast(f"Export failed: {e}", icon=":material/error:")
        
        tab1, tab2, tab3 = st.tabs([":material/analytics: Explorer", ":material/play_arrow: Active Pipelines", ":material/description: Logs"])
        
        with tab1:
            hcol1, hcol2, hcol3, hcol4 = st.columns([2, 1, 1, 1])
            
            with hcol1:
                st.markdown(
                    f"<p style='font-size:13px; color:#64748b; margin:6px 0 0;'>"
                    f"{len(t4_tools)} tools analyzed across {t4_stats.get('multi_domain', 0)} domains."
                    "</p>",
                    unsafe_allow_html=True,
                )
            
            with hcol2:
                auto_refresh_t4 = st.toggle("Auto (3s)", value=False, key="auto_refresh_t4")
            
            with hcol3:
                if st.button("Refresh", width="stretch", icon=":material/refresh:"):
                    st.rerun()
            
            with hcol4:
                if st.button("Export", width="stretch", icon=":material/download:"):
                    st.session_state._pending_t4_action = ("export_t4",)
                    st.rerun()
            
            st.markdown("<hr style='border-color:#1e2533; margin:8px 0;'>", unsafe_allow_html=True)
            
            fcol1, fcol2, fcol3 = st.columns(3)
            with fcol1:
                domain_filter = st.selectbox(
                    "Domain",
                    options=["All"] + list(CYBERSECURITY_DOMAINS),
                    index=0,
                    key="t4_domain_filter",
                )
            with fcol2:
                type_filter = st.selectbox(
                    "Type",
                    options=["All", "Enterprise", "Open Source", "Freemium"],
                    index=0,
                    key="t4_type_filter",
                )
            with fcol3:
                min_domains = st.slider(
                    "Min Domains", 1, 19, 1,
                    key="t4_min_domains"
                )
            
            filter_domain = None if domain_filter == "All" else domain_filter
            filter_type = None if type_filter == "All" else type_filter.lower().replace(" ", "_")
            
            filtered_tools = [
                t for t in t4_tools
                if (filter_domain is None or filter_domain in t.get("domain_list", []))
                and (filter_type is None or t.get("tool_type", "unknown") == filter_type)
                and t.get("domain_count", 0) >= min_domains
            ]
            
            st.markdown(f"**{len(filtered_tools)} tools**")
            
            col_list, col_detail = st.columns([1.2, 1.8], gap="medium")
            
            with col_list:
                if not filtered_tools:
                    st.info("No tools match the filters.")
                else:
                    import pandas as pd
                    rows = []
                    for t in filtered_tools:
                        rows.append({
                            "ID": t.get("id"),
                            "Vendor": t.get("vendor", ""),
                            "Product": t.get("product_name", ""),
                            "License": t.get("license_model", "Unknown"),
                            "Domains": t.get("domain_count", 0),
                            "Support": f"{t.get('support_rate', 0) * 100:.0f}%",
                        })
                    
                    df = pd.DataFrame(rows)
                    event = st.dataframe(
                        df,
                        width="stretch",
                        hide_index=True,
                        height=min(35 * len(rows) + 40, 400),
                        on_select="rerun",
                        selection_mode="single-row",
                        key="t4_table",
                    )
                    sel_rows = event.selection.get("rows", []) if event and event.selection else []
                    if sel_rows and 0 <= sel_rows[0] < len(rows):
                        st.session_state.t4_selected_tool_id = rows[sel_rows[0]]["ID"]
            
            with col_detail:
                sel_id = st.session_state.get("t4_selected_tool_id")
                if sel_id:
                    sel_tool = next((t for t in t4_tools if t.get("id") == sel_id), None)
                    if sel_tool:
                        st.markdown(f"**{sel_tool.get('vendor', '')} — {sel_tool.get('product_name', '')}**")
                        st.caption(f"License: {sel_tool.get('license_model', 'Unknown')}")
                        
                        if sel_tool.get("url"):
                            st.markdown(f"[Official Website]({sel_tool['url']})")
                        
                        support_pct = sel_tool.get("support_rate", 0) * 100
                        st.markdown(f"**Feature Support:** {support_pct:.1f}%")
                        st.progress(int(support_pct))
                        
                        st.markdown(f"**Domains ({sel_tool.get('domain_count', 0)}):**")
                        domain_list = sel_tool.get("domain_list", [])
                        st.markdown(", ".join(domain_list[:6]))
                        
                        with st.expander("Subdomain Breakdown", expanded=False):
                            subdomain_features = run_async(get_t4_tool_subdomain_features_list(sel_id))
                            for entry in subdomain_features:
                                pct = entry.get("support_pct", 0)
                                level = entry.get("support_level", "Unknown")
                                color = "#22c55e" if level == "High" else ("#eab308" if level == "Medium" else "#ef4444")
                                st.markdown(
                                    f"- **{entry.get('subdomain_name', '')}** ({entry.get('domain_name', '')}): "
                                    f"{entry.get('supported_subfeatures', 0)}/{entry.get('total_subfeatures', 0)} "
                                    f"[{pct:.0f}%] <span style='color:{color}'>{level}</span>",
                                    unsafe_allow_html=True,
                                )
                    else:
                        st.info("Select a tool from the list.")
                else:
                    st.info("Select a tool from the list to view details.")
            
            c1, c2, c3 = st.columns(3)
            with c1:
                if st.button("Run Analysis", width="stretch", icon=":material/play_arrow:"):
                    st.session_state._pending_t4_pipeline = (False,)
                    st.rerun()
            with c2:
                if st.button("Reset & Rebuild", width="stretch", icon=":material/delete_forever:"):
                    st.session_state._pending_t4_pipeline = (True,)
                    st.rerun()
            with c3:
                if st.button("Export All", width="stretch", icon=":material/download:"):
                    st.session_state._pending_t4_action = ("export_t4",)
                    st.rerun()
            
            if auto_refresh_t4:
                from streamlit_autorefresh import st_autorefresh
                st_autorefresh(interval=3000, key="ar_t4")
        
        with tab2:
            sync_pipeline_state()
            
            auto_refresh_t4_pipelines = st.toggle("Auto-refresh (3s)", value=False, key="auto_refresh_t4_pipelines")
            
            t4_pipeline = st.session_state.running_pipelines.get("t4_analysis")
            
            if not t4_pipeline:
                st.info("No T4 analysis is running. Click **Run Analysis** in the Explorer tab.")
            else:
                status = t4_pipeline.get("status", "queued")
                progress = t4_pipeline.get("progress", 0.0)
                step = t4_pipeline.get("step", "s1").upper()
                message = t4_pipeline.get("message", "")
                s_color = {"running": "#3b82f6", "done": "#22c55e", "failed": "#ef4444"}.get(status, "#94a3b8")
                
                with st.container(border=True):
                    h1, h2 = st.columns([4, 1])
                    with h1:
                        st.markdown("**T4 Tool-Level Analysis**")
                    with h2:
                        st.markdown(f"<span style='color:{s_color}; font-weight:600;'>{status.capitalize()}</span>", unsafe_allow_html=True)
                    
                    if status in ("running", "queued"):
                        st.progress(progress, text=f"[{step}] {message}" if status == "running" else "Waiting in queue...")
                        stages = [
                            ("S1", "Bootstrap", progress >= 0.05),
                            ("S2", "Enrich", progress >= 0.40),
                            ("S3", "Features", progress >= 0.65),
                            ("S4", "Domains", progress >= 0.85),
                        ]
                        sc = st.columns(4)
                        for i, (sl, slabel, done) in enumerate(stages):
                            sc[i].markdown(
                                f"<div style='text-align:center; font-size:11px; color:#64748b;'>"
                                f"{'✔' if done else '○'} <strong>{sl}</strong><br/>{slabel}</div>",
                                unsafe_allow_html=True,
                            )
                    elif status == "failed":
                        st.error(f"Analysis failed: {message or 'Unknown error'}")
                    elif status == "done":
                        st.success(f"Complete — {t4_stats.get('total', 0)} tools analyzed")
            
            if auto_refresh_t4_pipelines:
                from streamlit_autorefresh import st_autorefresh
                st_autorefresh(interval=3000, key="ar_t4_pipelines")
        
        with tab3:
            render_log_panel()

    elif current_nav == "Strategic Score Card (T5)":
        st.markdown(f"<h1 style='margin-bottom:0px; padding-bottom:8px;'>{icon_html('leaderboard', 'actions', 32)} Score Card</h1>", unsafe_allow_html=True)
        st.markdown("Unified multi-dimensional readiness score for canonical tools across 5 dimensions.")
        
        if st.session_state.status_message:
            st.toast(st.session_state.status_message)
            st.session_state.status_message = ""
            
        from db.t5_store import get_t5_scores, get_t5_stats, get_t5_run_status
        
        with st.spinner("Loading scores..."):
            t5_scores = run_async(get_t5_scores())
            t5_stats = run_async(get_t5_stats())
            t5_run = run_async(get_t5_run_status())
            
        sync_pipeline_state()
        
        pending_t5 = st.session_state.pop("_pending_t5_action", None)
        if pending_t5:
            action_type = pending_t5[0]
            if action_type == "export_t5":
                try:
                    from excel.t5_bridge import export_t5_workbook
                    output_path = run_async(export_t5_workbook())
                    st.session_state.last_export_path = output_path
                    st.toast("Excel exported successfully", icon=":material/check_circle:")
                except Exception as e:
                    st.toast(f"Export failed: {e}", icon=":material/error:")
            elif action_type == "stop_t5_pipeline":
                pipeline_key = "t5_scorecard"
                stop_flag = _PIPELINE_STOP_FLAGS.get(pipeline_key)
                if stop_flag:
                    stop_flag.set()
                future = _PIPELINE_FUTURES.get(pipeline_key)
                if future and not future.done():
                    future.cancel()
                with _PIPELINE_LOCK:
                    if pipeline_key in _PIPELINE_PROGRESS:
                        _PIPELINE_PROGRESS[pipeline_key]["status"] = "stopped"
                        _PIPELINE_PROGRESS[pipeline_key]["message"] = "Stopped by user"
                st.toast("Pipeline stop requested", icon=":material/stop_circle:")
            elif action_type == "reset_t5_data":
                pipeline_key = "t5_scorecard"
                # Stop any running pipeline first
                stop_flag = _PIPELINE_STOP_FLAGS.get(pipeline_key)
                if stop_flag:
                    stop_flag.set()
                future = _PIPELINE_FUTURES.get(pipeline_key)
                if future and not future.done():
                    future.cancel()
                # Clear in-memory state
                with _PIPELINE_LOCK:
                    _PIPELINE_PROGRESS.pop(pipeline_key, None)
                st.session_state.running_pipelines.pop(pipeline_key, None)
                st.session_state.pop("t5_selected_tool_id", None)
                # Clear database
                from db.t5_store import reset_t5_data
                run_async(reset_t5_data())
                st.toast("T5 data cleared — all scores, ranks and insights removed.", icon=":material/delete_forever:")

        tab1, tab2, tab3 = st.tabs([":material/analytics: Explorer", ":material/play_arrow: Active Pipelines", ":material/description: Logs"])
        
        with tab1:
            hcol1, hcol2, hcol3, hcol4 = st.columns([2, 1, 1, 1])
            with hcol1:
                st.markdown(
                    f"<p style='font-size:13px; color:#64748b; margin:6px 0 0;'>"
                    f"{len(t5_scores)} tools scored. Avg Composite: {t5_stats.get('avg_composite') or 0.0:.1f}"
                    "</p>",
                    unsafe_allow_html=True,
                )
            with hcol2:
                auto_refresh_t5 = st.toggle("Auto (3s)", value=False, key="auto_refresh_t5")
            with hcol3:
                if st.button("Refresh", width="stretch", icon=":material/refresh:", key="t5_refresh"):
                    st.rerun()
            with hcol4:
                if st.button("Export", width="stretch", icon=":material/download:", key="t5_export"):
                    st.session_state._pending_t5_action = ("export_t5",)
                    st.rerun()
                    
            st.markdown("<hr style='border-color:#1e2533; margin:8px 0;'>", unsafe_allow_html=True)
            
            fcol1, fcol2, fcol3 = st.columns(3)
            with fcol1:
                grade_filter = st.selectbox("Grade", options=["All", "A+", "A", "B+", "B", "C", "D"], key="t5_grade_filter")
            with fcol2:
                min_score = st.slider("Min Composite Score", 0, 100, 0, key="t5_min_score")
            with fcol3:
                domains = ["All"] + sorted(list(set(s.get("primary_domain", "") for s in t5_scores if s.get("primary_domain"))))
                domain_filter = st.selectbox("Domain", options=domains, key="t5_domain_filter")
                
            filtered_scores = [
                s for s in t5_scores
                if (grade_filter == "All" or s.get("grade") == grade_filter)
                and (domain_filter == "All" or s.get("primary_domain") == domain_filter)
                and s.get("composite_score", 0) >= min_score
            ]
            
            col_list, col_detail = st.columns([1.2, 1.8], gap="medium")
            
            with col_list:
                if not filtered_scores:
                    st.info("No scores match the filters.")
                else:
                    import pandas as pd
                    rows = []
                    for s in filtered_scores:
                        rows.append({
                            "T4_ID": s.get("t4_tool_id"),
                            "Domain": s.get("primary_domain", ""),
                            "Rank": s.get("domain_rank", "-"),
                            "Vendor": s.get("vendor", ""),
                            "Product": s.get("product_name", ""),
                            "Category": s.get("tool_category", ""),
                            "Grade": s.get("grade", "-"),
                            "Score": f"{s.get('composite_score', 0):.1f}"
                        })
                    df = pd.DataFrame(rows)
                    event = st.dataframe(
                        df.drop(columns=["T4_ID"]),
                        width="stretch", hide_index=True, height=min(35 * len(rows) + 40, 400),
                        on_select="rerun", selection_mode="single-row", key="t5_table"
                    )
                    sel_rows = event.selection.get("rows", []) if event and event.selection else []
                    if sel_rows and 0 <= sel_rows[0] < len(rows):
                        st.session_state.t5_selected_tool_id = rows[sel_rows[0]]["T4_ID"]
            
            with col_detail:
                sel_id = st.session_state.get("t5_selected_tool_id")
                if sel_id:
                    sel_tool = next((s for s in t5_scores if s.get("t4_tool_id") == sel_id), None)
                    if sel_tool:
                        grade = sel_tool.get("grade", "-")
                        c_grade = {"A+": "#15803d", "A": "#16a34a", "B+": "#ca8a04", "B": "#eab308", "C": "#ea580c", "D": "#b91c1c"}.get(grade, "#94a3b8")
                        
                        st.markdown(f"**{sel_tool.get('vendor', '')} — {sel_tool.get('product_name', '')}**")
                        st.markdown(
                            f"<span style='font-size:24px; font-weight:bold; color:{c_grade}'>{grade}</span>"
                            f"<span style='font-size:18px; color:#f8fafc; margin-left:12px;'>{sel_tool.get('composite_score', 0):.1f} / 100</span>",
                            unsafe_allow_html=True
                        )
                        
                        if sel_tool.get("quadrant_position"):
                            st.markdown(f"**Quadrant:** {sel_tool['quadrant_position']}")
                            
                        if sel_tool.get("strategic_insight"):
                            st.info(sel_tool["strategic_insight"], icon=":material/psychiatry:")
                            
                        st.markdown("**Dimensions**")
                        dims = [
                            ("D1: Feature Coverage", sel_tool.get("d1_feature_coverage", 0)),
                            ("D2: Domain Breadth", sel_tool.get("d2_domain_breadth", 0)),
                            ("D3: NIST Alignment", sel_tool.get("d3_nist_alignment", 0)),
                            ("D4: Market Maturity", sel_tool.get("d4_market_maturity", 0)),
                            ("D5: Ranking Signal", sel_tool.get("d5_ranking_signal", 0))
                        ]
                        for name, val in dims:
                            st.markdown(f"<div style='font-size:12px; margin-bottom:2px;'>{name} <b>{val:.1f}</b></div>", unsafe_allow_html=True)
                            st.progress(int(val))
                    else:
                        st.info("Select a tool from the list.")
                else:
                    st.info("Select a tool from the list to view Score Card details.")
                    
            c1, c2, c3 = st.columns(3)
            with c1:
                if st.button("Run Score Card", width="stretch", icon=":material/play_arrow:", key="t5_run"):
                    st.session_state._pending_t5_pipeline = (False,)
                    st.rerun()
            with c2:
                if st.button("Reset Data", width="stretch", icon=":material/delete_forever:", key="t5_reset"):
                    st.session_state._pending_t5_action = ("reset_t5_data",)
                    st.rerun()
            with c3:
                pass
                
            if auto_refresh_t5:
                from streamlit_autorefresh import st_autorefresh
                st_autorefresh(interval=3000, key="ar_t5")
                
        with tab2:
            sync_pipeline_state()
            
            auto_refresh_t5_pipelines = st.toggle("Auto-refresh (3s)", value=False, key="auto_refresh_t5_pipelines")
            
            t5_pipeline = st.session_state.running_pipelines.get("t5_scorecard")
            
            if not t5_pipeline:
                st.info("No T5 Score Card pipeline is running.")
            else:
                status = t5_pipeline.get("status", "queued")
                progress = t5_pipeline.get("progress", 0.0)
                step = t5_pipeline.get("step", "s1").upper()
                message = t5_pipeline.get("message", "")
                s_color = {"running": "#3b82f6", "done": "#22c55e", "failed": "#ef4444", "stopped": "#f59e0b"}.get(status, "#94a3b8")
                
                with st.container(border=True):
                    h1, h2 = st.columns([4, 1])
                    with h1:
                        st.markdown("**T5 Score Card**")
                    with h2:
                        st.markdown(f"<span style='color:{s_color}; font-weight:600;'>{status.capitalize()}</span>", unsafe_allow_html=True)
                        
                    if status in ("running", "queued"):
                        st.progress(progress, text=f"[{step}] {message}" if status == "running" else "Waiting in queue...")
                        stages = [("S1", "Init", progress > 0.1), ("S2/3", "Scoring", progress > 0.5), ("S4", "LLM", progress > 0.8), ("S5", "Excel", progress >= 0.95)]
                        sc = st.columns(4)
                        for i, (sl, slabel, done) in enumerate(stages):
                            sc[i].markdown(
                                f"<div style='text-align:center; font-size:11px; color:#64748b;'>"
                                f"{'✔' if done else '○'} <strong>{sl}</strong><br/>{slabel}</div>",
                                unsafe_allow_html=True,
                            )
                        st.markdown("")
                        if st.button(
                            "Stop Pipeline",
                            icon=":material/stop_circle:",
                            width="content",
                            key="t5_stop_btn",
                            type="primary",
                        ):
                            st.session_state._pending_t5_action = ("stop_t5_pipeline",)
                            st.rerun()
                    elif status == "failed":
                        st.error(f"Failed: {message or 'Unknown error'}")
                    elif status == "stopped":
                        st.warning(f"Stopped by user at {int(progress * 100)}% — partial scores may be saved.")
                    elif status == "done":
                        st.success(f"Complete — {t5_stats.get('total', 0)} tools scored")
                        
            if auto_refresh_t5_pipelines:
                from streamlit_autorefresh import st_autorefresh
                st_autorefresh(interval=3000, key="ar_t5_pipelines")

        with tab3:
            render_log_panel()

    pending_pipeline = st.session_state.pop("_pending_t2_pipeline", None)

    if pending_pipeline:
        subdomain_id, subdomain_name = pending_pipeline
        event_queue = st.session_state.event_queue
        if not event_queue:
            from orchestrator.subdomain_graph import create_event_queue as create_t2_event_queue
            event_queue = create_t2_event_queue()
            st.session_state.event_queue = event_queue
        
        pipeline_key = f"t2_{subdomain_name}"
        
        initial = {
            "subdomain": subdomain_name,
            "progress": 0.0,
            "step": "d1",
            "message": "Queued...",
            "status": "queued",
        }
        with _PIPELINE_LOCK:
            _PIPELINE_PROGRESS[pipeline_key] = initial
        st.session_state.running_pipelines[pipeline_key] = dict(initial)
        
        def _run_t2_in_thread(_subdomain_id=subdomain_id, _subdomain_name=subdomain_name, _queue=event_queue, _pkey=pipeline_key):
            with _PIPELINE_LOCK:
                if _pkey in _PIPELINE_PROGRESS:
                    _PIPELINE_PROGRESS[_pkey]["status"] = "running"
                    _PIPELINE_PROGRESS[_pkey]["message"] = "Starting..."
            import asyncio
            from orchestrator.subdomain_graph import run_subdomain_pipeline
            
            async def _run_with_progress():
                task = asyncio.create_task(run_subdomain_pipeline(_subdomain_id, _subdomain_name, _queue))
                
                while not task.done():
                    try:
                        event = await asyncio.wait_for(_queue.get(), timeout=settings.event_timeout)
                        if event.subdomain == _subdomain_name:
                            with _PIPELINE_LOCK:
                                if _pkey in _PIPELINE_PROGRESS:
                                    _PIPELINE_PROGRESS[_pkey].update({
                                        "progress": event.progress_pct,
                                        "step": event.step,
                                        "message": event.message,
                                        "status": "running",
                                    })
                    except asyncio.TimeoutError:
                        continue
                
                with _PIPELINE_LOCK:
                    if _pkey in _PIPELINE_PROGRESS:
                        _PIPELINE_PROGRESS[_pkey]["status"] = "done"
            
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(_run_with_progress())
            except Exception as exc:
                logger.error(f"T2 Pipeline failed for {_subdomain_name}: {exc}")
                with _PIPELINE_LOCK:
                    if _pkey in _PIPELINE_PROGRESS:
                        _PIPELINE_PROGRESS[_pkey]["status"] = "failed"
            finally:
                loop.close()
        
        future = _EXECUTOR.submit(_run_t2_in_thread)
        _PIPELINE_FUTURES[pipeline_key] = future
        
        st.toast(f"Started ranking for {subdomain_name}")
        st.rerun()

    # ── T3: pending pipeline handler (runs outside nav branch so it fires every render) ──
    pending_t3 = st.session_state.pop("_pending_t3_pipeline", None)
    if pending_t3:
        reset_t3 = pending_t3[0]
        event_queue = st.session_state.event_queue
        pipeline_key = "t3_classification"

        # Guard: don't start a second thread if T3 is already actively running
        existing_t3 = st.session_state.running_pipelines.get(pipeline_key, {})
        if existing_t3.get("status") == "running" and not reset_t3:
            st.toast("T3 classification is already running", icon=":material/info:")
            st.rerun()

        initial = {
            "subdomain": "t3_classification",
            "progress": 0.0,
            "step": "s1",
            "message": "Queued...",
            "status": "queued",
        }
        with _PIPELINE_LOCK:
            _PIPELINE_PROGRESS[pipeline_key] = initial
            st.session_state.running_pipelines[pipeline_key] = dict(initial)

        def _run_t3_in_thread(_queue=event_queue, _pkey=pipeline_key, _reset=reset_t3):
            with _PIPELINE_LOCK:
                if _pkey in _PIPELINE_PROGRESS:
                    _PIPELINE_PROGRESS[_pkey]["status"] = "running"
                    _PIPELINE_PROGRESS[_pkey]["message"] = "Starting..."
            import asyncio
            from orchestrator.t3_graph import run_t3_pipeline

            async def _run():
                task = asyncio.create_task(run_t3_pipeline(_queue, reset_existing=_reset))
                while not task.done():
                    try:
                        event = await asyncio.wait_for(_queue.get(), timeout=settings.event_timeout)
                        if event.subdomain == "t3_classification":
                            with _PIPELINE_LOCK:
                                if _pkey in _PIPELINE_PROGRESS:
                                    _PIPELINE_PROGRESS[_pkey].update({
                                        "progress": event.progress_pct,
                                        "step": event.step,
                                        "message": event.message,
                                        "status": "running",
                                    })
                    except asyncio.TimeoutError:
                        continue
                with _PIPELINE_LOCK:
                    if _pkey in _PIPELINE_PROGRESS:
                        try:
                            success = task.result()
                        except Exception:
                            success = False
                        _PIPELINE_PROGRESS[_pkey]["status"] = "done" if success else "failed"

            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(_run())
            except Exception as exc:
                logger.error(f"T3 pipeline thread failed: {exc}")
                with _PIPELINE_LOCK:
                    if _pkey in _PIPELINE_PROGRESS:
                        _PIPELINE_PROGRESS[_pkey]["status"] = "failed"
            finally:
                loop.close()

        future = _EXECUTOR.submit(_run_t3_in_thread)
        _PIPELINE_FUTURES[pipeline_key] = future
        st.toast("T3 classification started")
        st.rerun()

    # ── T4: pending pipeline handler (runs outside nav branch so it fires every render) ──
    pending_t4 = st.session_state.pop("_pending_t4_pipeline", None)
    if pending_t4:
        reset_t4 = pending_t4[0]
        event_queue = st.session_state.event_queue
        pipeline_key = "t4_analysis"

        existing_t4 = st.session_state.running_pipelines.get(pipeline_key, {})
        if existing_t4.get("status") == "running" and not reset_t4:
            st.toast("T4 analysis is already running", icon=":material/info:")
            st.rerun()

        initial = {
            "subdomain": "t4_analysis",
            "progress": 0.0,
            "step": "s1",
            "message": "Queued...",
            "status": "queued",
        }
        with _PIPELINE_LOCK:
            _PIPELINE_PROGRESS[pipeline_key] = initial
            st.session_state.running_pipelines[pipeline_key] = dict(initial)

        def _run_t4_in_thread(_queue=event_queue, _pkey=pipeline_key, _reset=reset_t4):
            with _PIPELINE_LOCK:
                if _pkey in _PIPELINE_PROGRESS:
                    _PIPELINE_PROGRESS[_pkey]["status"] = "running"
                    _PIPELINE_PROGRESS[_pkey]["message"] = "Starting..."
            import asyncio
            from orchestrator.t4_graph import run_t4_pipeline

            async def _run():
                task = asyncio.create_task(run_t4_pipeline(_queue, reset_existing=_reset))
                while not task.done():
                    try:
                        event = await asyncio.wait_for(_queue.get(), timeout=settings.event_timeout)
                        if event.subdomain == "t4_analysis":
                            with _PIPELINE_LOCK:
                                if _pkey in _PIPELINE_PROGRESS:
                                    _PIPELINE_PROGRESS[_pkey].update({
                                        "progress": event.progress_pct,
                                        "step": event.step,
                                        "message": event.message,
                                        "status": "running",
                                    })
                    except asyncio.TimeoutError:
                        continue
                with _PIPELINE_LOCK:
                    if _pkey in _PIPELINE_PROGRESS:
                        try:
                            success = task.result()
                        except Exception:
                            success = False
                        _PIPELINE_PROGRESS[_pkey]["status"] = "done" if success else "failed"

            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(_run())
            except Exception as exc:
                logger.error(f"T4 pipeline thread failed: {exc}")
                with _PIPELINE_LOCK:
                    if _pkey in _PIPELINE_PROGRESS:
                        _PIPELINE_PROGRESS[_pkey]["status"] = "failed"
            finally:
                loop.close()

        future = _EXECUTOR.submit(_run_t4_in_thread)
        _PIPELINE_FUTURES[pipeline_key] = future
        st.toast("T4 analysis started")
        st.rerun()

    # ── T5: pending pipeline handler ──
    pending_t5 = st.session_state.pop("_pending_t5_pipeline", None)
    if pending_t5:
        reset_t5 = pending_t5[0]
        pipeline_key = "t5_scorecard"

        existing_t5 = st.session_state.running_pipelines.get(pipeline_key, {})
        if existing_t5.get("status") == "running" and not reset_t5:
            st.toast("T5 Score Card is already running", icon=":material/info:")
            st.rerun()

        initial = {
            "subdomain": "t5_scorecard",
            "progress": 0.0,
            "step": "s1",
            "message": "Queued...",
            "status": "queued",
        }
        with _PIPELINE_LOCK:
            _PIPELINE_PROGRESS[pipeline_key] = initial
            st.session_state.running_pipelines[pipeline_key] = dict(initial)

        # Clear any old stop flag and create a fresh one for this run
        stop_event = threading.Event()
        _PIPELINE_STOP_FLAGS[pipeline_key] = stop_event

        def _run_t5_in_thread(_pkey=pipeline_key, _reset=reset_t5, _stop=stop_event):
            with _PIPELINE_LOCK:
                if _pkey in _PIPELINE_PROGRESS:
                    _PIPELINE_PROGRESS[_pkey]["status"] = "running"
                    _PIPELINE_PROGRESS[_pkey]["message"] = "Starting..."
            import asyncio
            from orchestrator.t5_graph import run_t5_pipeline

            async def _run():
                _queue = asyncio.Queue()
                task = asyncio.create_task(run_t5_pipeline(_queue, reset_existing=_reset))
                while not task.done():
                    # Check for user-requested stop
                    if _stop.is_set():
                        task.cancel()
                        with _PIPELINE_LOCK:
                            if _pkey in _PIPELINE_PROGRESS:
                                _PIPELINE_PROGRESS[_pkey]["status"] = "stopped"
                                _PIPELINE_PROGRESS[_pkey]["message"] = "Stopped by user"
                        logger.info(f"T5 pipeline cancelled by user")
                        break
                    try:
                        event = await asyncio.wait_for(_queue.get(), timeout=settings.event_timeout)
                        if event.subdomain == "t5_scorecard":
                            with _PIPELINE_LOCK:
                                if _pkey in _PIPELINE_PROGRESS:
                                    _PIPELINE_PROGRESS[_pkey].update({
                                        "progress": event.progress_pct,
                                        "step": event.step,
                                        "message": event.message,
                                        "status": "running",
                                    })
                    except asyncio.TimeoutError:
                        continue
                with _PIPELINE_LOCK:
                    if _pkey in _PIPELINE_PROGRESS and _PIPELINE_PROGRESS[_pkey]["status"] not in ("stopped",):
                        try:
                            success = task.result()
                        except Exception:
                            success = False
                        _PIPELINE_PROGRESS[_pkey]["status"] = "done" if success else "failed"

            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(_run())
            except Exception as exc:
                logger.error(f"T5 pipeline thread failed: {exc}")
                with _PIPELINE_LOCK:
                    if _pkey in _PIPELINE_PROGRESS:
                        _PIPELINE_PROGRESS[_pkey]["status"] = "failed"
            finally:
                loop.close()

        future = _EXECUTOR.submit(_run_t5_in_thread)
        _PIPELINE_FUTURES[pipeline_key] = future
        st.toast("T5 Score Card started")
        st.rerun()


if __name__ == "__main__":
    main()
