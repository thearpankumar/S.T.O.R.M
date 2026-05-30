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

_PIPELINE_THREADS: dict[str, threading.Thread] = {}


def _cleanup_thread(thread: threading.Thread, timeout: float = 5.0) -> None:
    if thread.is_alive():
        thread.join(timeout=timeout)


import atexit
def _shutdown_all_threads() -> None:
    for name, thread in list(_PIPELINE_THREADS.items()):
        if thread.is_alive():
            thread.join(timeout=2.0)
atexit.register(_shutdown_all_threads)

@st.cache_resource
def get_pipeline_progress() -> dict[str, dict]:
    return {}

@st.cache_resource
def get_pipeline_lock() -> threading.Lock:
    return threading.Lock()

_PIPELINE_PROGRESS = get_pipeline_progress()
_PIPELINE_LOCK = get_pipeline_lock()


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
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = copy.deepcopy(value)


def get_event_loop() -> asyncio.AbstractEventLoop:
    try:
        loop = asyncio.get_running_loop()
        return loop
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    try:
        loop = asyncio.get_running_loop()
        return loop.run_until_complete(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


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


async def export_all_async() -> str:
    try:
        from excel.bridge import export_all_subdomains
        return await export_all_subdomains()
    except ImportError as e:
        logger.error(f"Failed to import excel bridge: {e}")
        raise RuntimeError("Excel export module not available") from e


async def stop_all_workers_async() -> int:
    running_count = sum(
        1 for p in st.session_state.running_pipelines.values()
        if p.get("status") == "running"
    )
    st.session_state.running_pipelines = {}
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
    </style>
    """, unsafe_allow_html=True)


def render_sidebar() -> str:
    with st.sidebar:
        search_svg = icon_html("search", "actions", 20)
        st.markdown(f"<h3 style='margin-bottom:0;'>{search_svg} Cybersec Research</h3>", unsafe_allow_html=True)
        
        st.markdown("---")
        nav_selection = st.selectbox(
            "Techniques",
            options=["Technique 1", "Technique 2"],
            index=0,
            label_visibility="collapsed",
        )
        
        st.markdown("---")
        st.markdown("**Config**")
        st.caption(f"Max Workers: `{settings.max_workers}`")
        st.caption(f"DB: `{settings.db_path}`")
        st.caption(f"Output: `{settings.excel_output_path}`")
        st.markdown("---")
        st.caption("Use the Explorer tab controls to Refresh, Stop workers, or Export.")
        
        st.markdown(
            """
            <style>
            [data-testid="stSidebar"] {
                position: relative;
            }
            [data-testid="stSidebarUserContent"] {
                padding-bottom: 120px;
            }
            .st-key-sidebar_bottom {
                position: absolute;
                bottom: 20px;
                width: 85%;
            }
            </style>
            """,
            unsafe_allow_html=True,
        )

        with st.container(key="sidebar_bottom"):
            if st.button("Read Documentation", use_container_width=True, icon=":material/description:"):
                st.session_state.show_docs = True

            if st.button("Settings", use_container_width=True, icon=":material/settings:"):
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
                submitted = st.form_submit_button("Save Settings", type="primary", use_container_width=True)
            with c2:
                cancel = st.form_submit_button("Cancel", use_container_width=True)
                
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
        if st.button("Refresh", use_container_width=True, key="log_refresh", icon=":material/refresh:"):
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
                "message":        "Starting…",
                "status":         "running",
            }
            with _PIPELINE_LOCK:
                _PIPELINE_PROGRESS[pipeline_key] = initial
            st.session_state.running_pipelines[pipeline_key] = dict(initial)

            st.session_state.status_message = (
                f"Pipeline started: {subdomain_name} - check the Active Pipelines tab for live progress"
            )
            st.session_state.status_type = "info"

            def _run_in_thread(
                _domain=domain,
                _sid=subdomain_id,
                _sname=subdomain_name,
                _queue=event_queue,
            ):
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

            t = threading.Thread(target=_run_in_thread, daemon=False, name=f"pipeline-{subdomain_name}")
            _PIPELINE_THREADS[pipeline_key] = t
            t.start()

    elif action_type == "export":
        subdomain_id, subdomain_name = pending[1], pending[2]
        try:
            output_path = run_async(export_subdomain_async(subdomain_id, subdomain_name))
            st.toast(f"Exported to: {output_path}", icon=":material/check_circle:")
        except Exception as e:
            st.toast(f"Export failed: {e}", icon=":material/error:")

    elif action_type == "export_all":
        try:
            output_path = run_async(export_all_async())
            st.session_state.status_message = f"Exported to: {output_path}"
            st.session_state.status_type = "success"
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
        if st.button("Refresh", use_container_width=True, help="Clear completed pipelines and refresh", icon=":material/refresh:"):
            cleanup_completed_pipelines()
            st.session_state.status_message = "Refreshed"
            st.session_state.status_type = "info"
            st.rerun()

    with hcol3:
        if st.button("Stop All", use_container_width=True, help="Stop all running workers", icon=":material/stop:"):
            count = run_async(stop_all_workers_async())
            st.session_state.status_message = f"Stopped {count} workers"
            st.session_state.status_type = "warning"
            st.rerun()

    with hcol4:
        if st.button("Export All", use_container_width=True, help="Export all completed subdomains", icon=":material/bar_chart:"):
            try:
                output_path = run_async(export_all_async())
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

    if current_nav == "Technique 1":
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

            running_pipelines = st.session_state.running_pipelines

            if not running_pipelines:
                st.info("No pipelines are currently tracked. Start one from the Explorer tab.")
            else:
                for key, pipeline in list(running_pipelines.items()):
                    status       = pipeline.get("status", "pending")
                    sname        = pipeline.get("subdomain_name", key)
                    domain       = pipeline.get("domain", "")
                    progress     = pipeline.get("progress", 0.0)
                    step         = pipeline.get("step", "m2").upper()
                    message      = pipeline.get("message", "")

                    status_cfg = {
                        "running": (status_icon("running", 14), "#3b82f6"),
                        "done": (status_icon("done", 14), "#22c55e"),
                        "failed": (status_icon("failed", 14), "#ef4444"),
                    }.get(status, (status_icon("pending", 14), "#6b7280"))
                    s_icon, s_color = status_cfg

                    # Stage labels for the 4-step pipeline
                    STAGE_LABELS = {
                        "m2": "Tool Discovery",
                        "m3": "Feature Discovery",
                        "m4": "Sub-feature Discovery",
                        "m5": "Matrix Population",
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

                        if status == "running":
                            st.progress(progress, text=f"[{step}] {step_label}")
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
                if st.button("Clear Completed / Failed", use_container_width=True, icon=":material/delete:"):
                    cleanup_completed_pipelines()
                    st.rerun()
            with cc2:
                if st.button("Refresh Now", use_container_width=True, icon=":material/refresh:"):
                    sync_pipeline_state()
                    st.rerun()

            if auto_refresh:
                st.markdown("""
                <script>
                setTimeout(function() {
                    var refreshBtn = window.parent.document.querySelector('[data-testid="stBaseButton-secondary"]');
                    if (refreshBtn && refreshBtn.innerText.includes('Refresh')) {
                        refreshBtn.click();
                    } else {
                        window.parent.location.reload();
                    }
                }, 3000);
                </script>
                """, unsafe_allow_html=True)

        # ── Logs tab ───────────────────────────────────────────────────────────────
        with tab3:
            render_log_panel()

    else:
        st.markdown(f"# {current_nav}")
        st.info("This technique module will be added in a future update!")


if __name__ == "__main__":
    try:
        main()
    finally:
        run_async(shutdown_app_async())
