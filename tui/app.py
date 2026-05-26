from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Header, Footer, Static, Tree, Button, ProgressBar
from textual import on, work
from textual.reactive import reactive
from textual.worker import Worker

import asyncio

from models.worker import WorkerEvent
from config.domains import CYBERSECURITY_DOMAINS
from db.store import db, get_domain_id, get_subdomains, init_db, shutdown_db
from orchestrator.graph import run_worker_pipeline, create_event_queue
from tui.log_panel import LogPanel

import logging

logger = logging.getLogger(__name__)


class DomainTree(Tree[str]):
    def __init__(self):
        super().__init__("", id="domain-tree")
        self.show_root = False
        self.guide_depth = 2
        
    async def load_domains(self) -> None:
        self.clear()
        root = self.root
        root.expand()
        
        for domain in CYBERSECURITY_DOMAINS:
            await asyncio.sleep(0)
            domain_id = await get_domain_id(domain)
            if domain_id:
                subdomains = await get_subdomains(domain_id)
                node = root.add(f"[{domain}]", data={
                    "type": "domain",
                    "name": domain,
                    "id": domain_id
                })
                for sd in subdomains:
                    status_icon = self._get_status_icon(sd["status"])
                    node.add_leaf(f"{status_icon} {sd['name']}", data={
                        "type": "subdomain",
                        "id": sd["id"],
                        "name": sd["name"],
                        "status": sd["status"],
                        "domain": domain
                    })
                if not subdomains:
                    node.add_leaf("[dim]No subdomains yet[/dim]", data=None)
    
    def _get_status_icon(self, status: str) -> str:
        icons = {
            "pending": "[yellow]○[/yellow]",
            "running": "[blue]◐[/blue]",
            "done": "[green]●[/green]",
            "failed": "[red]✗[/red]"
        }
        return icons.get(status, "○")


class WorkerCard(Static):
    def __init__(self, subdomain: str):
        super().__init__(classes="worker-card")
        self.subdomain = subdomain
        self.progress = 0.0
        self.step = ""
        self.message = ""
        
    def compose(self) -> ComposeResult:
        yield Static(self.subdomain, classes="worker-title")
        yield ProgressBar(total=100, classes="worker-progress")
        yield Static("", id=f"worker-status-{id(self)}", classes="worker-status")
        
    def update_progress(self, progress_pct: float, step: str, message: str) -> None:
        self.progress = progress_pct
        self.step = step
        self.message = message
        try:
            pb = self.query_one(ProgressBar)
            pb.update(progress=int(progress_pct * 100))
            status = self.query_one(f"#worker-status-{id(self)}")
            status.update(f"[{step}] {message}")
        except Exception as e:
            logger.warning(f"Failed to update progress: {e}")


class DiscoveryView(Container):
    def compose(self) -> ComposeResult:
        yield Static("Discovery View", classes="view-title")
        yield DomainTree()
        yield Horizontal(
            Button("Discover Selected", id="btn-discover", variant="primary"),
            Button("Discover All", id="btn-discover-all", variant="warning"),
            Button("Stop All", id="btn-stop", variant="error"),
            Button("Refresh", id="btn-refresh", variant="default"),
            classes="button-row"
        )
        yield Horizontal(
            Button("Export Selected", id="btn-export-selected", variant="success"),
            Button("Export All", id="btn-export-all", variant="success"),
            classes="button-row"
        )
        yield Static("", id="discovery-status", classes="status-bar")
        
    async def on_mount(self) -> None:
        tree = self.query_one(DomainTree)
        await tree.load_domains()
        
    @on(Button.Pressed, "#btn-discover")
    def on_discover_selected(self) -> None:
        tree = self.query_one(DomainTree)
        selected = tree.cursor_node
        status = self.query_one("#discovery-status")
        app = self.app
        
        if not selected or not selected.data:
            status.update("[red]No selection[/red]")
            return
        
        if isinstance(selected.data, dict) and selected.data.get("type") == "domain":
            domain_name = selected.data.get("name")
            status.update(f"[yellow]Discovering subdomains for {domain_name}...[/yellow]")
            self._discover_subdomains_for_domain(domain_name)
                
        elif isinstance(selected.data, dict) and selected.data.get("type") == "subdomain":
            subdomain_data = selected.data
            status.update(f"[yellow]Starting worker for {subdomain_data['name']}...[/yellow]")
            self._run_subdomain_worker(
                subdomain_data["domain"],
                subdomain_data["id"],
                subdomain_data["name"]
            )
        else:
            status.update("[red]Please select a domain or subdomain[/red]")
    
    @on(Button.Pressed, "#btn-stop")
    async def on_stop_all(self) -> None:
        status = self.query_one("#discovery-status")
        app = self.app
        
        workers_cancelled = self.app.workers.cancel_all()
        
        from db.store import db
        await db.execute(
            "UPDATE subdomains SET status = 'pending' WHERE status = 'running'"
        )
        await db.commit()
        
        tree = self.query_one(DomainTree)
        await tree.load_domains()
        
        status.update(f"[red]Stopped {workers_cancelled} workers. All subdomains reset to pending.[/red]")
        if hasattr(app, "update_status"):
            app.update_status(f"Stopped {workers_cancelled} workers", is_error=True)
    
    @work(exclusive=True)
    async def _discover_subdomains_for_domain(self, domain_name: str) -> None:
        from agents.discovery import discover_subdomains
        
        status = self.query_one("#discovery-status")
        app = self.app
        
        try:
            subdomains = await discover_subdomains(domain_name)
            
            tree = self.query_one(DomainTree)
            await tree.load_domains()
            
            status.update(f"[green]Discovered {len(subdomains)} subdomains for {domain_name}[/green]")
            
            if hasattr(app, "update_status"):
                app.update_status(f"Discovered {len(subdomains)} subdomains for {domain_name}")
        except Exception as e:
            status.update(f"[red]Error: {e}[/red]")
            if hasattr(app, "update_status"):
                app.update_status(f"Error: {e}", is_error=True)
    
    @work(exclusive=False)
    async def _run_subdomain_worker(self, domain: str, subdomain_id: int, subdomain_name: str) -> None:
        from orchestrator.graph import run_worker_pipeline
        from db.store import update_subdomain_status
        
        status = self.query_one("#discovery-status")
        tree = self.query_one(DomainTree)
        app = self.app
        
        await update_subdomain_status(subdomain_id, "running")
        await tree.load_domains()
        status.update(f"[blue]Running: {subdomain_name}...[/blue]")
        
        if hasattr(app, "_event_queue") and app._event_queue:
            try:
                await run_worker_pipeline(domain, subdomain_id, subdomain_name, app._event_queue)
                await tree.load_domains()
                status.update(f"[green]Completed: {subdomain_name}[/green]")
                if hasattr(app, "update_status"):
                    app.update_status(f"Completed: {subdomain_name}")
            except Exception as e:
                await tree.load_domains()
                status.update(f"[red]Failed: {subdomain_name} - {e}[/red]")
                if hasattr(app, "update_status"):
                    app.update_status(f"Failed: {subdomain_name}", is_error=True)
        else:
            status.update("[red]Event queue not initialized[/red]")
    
    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        logger.debug(f"Worker state changed: {event.worker.name} -> {event.state}")
    
    @on(Button.Pressed, "#btn-export-selected")
    async def on_export_selected(self) -> None:
        from excel.bridge import create_workbook_from_db
        
        tree = self.query_one(DomainTree)
        selected = tree.cursor_node
        status = self.query_one("#discovery-status")
        
        if not selected or not selected.data:
            status.update("[red]No selection[/red]")
            return
        
        if isinstance(selected.data, dict) and selected.data.get("type") == "subdomain":
            sd_data = selected.data
            if sd_data.get("status") != "done":
                status.update("[red]Subdomain not completed yet[/red]")
                return
            
            status.update(f"[yellow]Exporting {sd_data['name']}...[/yellow]")
            try:
                await create_workbook_from_db(sd_data["id"], sd_data["name"])
                from config.settings import settings
                status.update(f"[green]Exported to {settings.excel_output_path}[/green]")
            except Exception as e:
                status.update(f"[red]Export failed: {e}[/red]")
        else:
            status.update("[red]Please select a completed subdomain[/red]")
    
    @on(Button.Pressed, "#btn-export-all")
    async def on_export_all(self) -> None:
        from excel.bridge import export_all_subdomains
        
        status = self.query_one("#discovery-status")
        status.update("[yellow]Exporting all completed subdomains...[/yellow]")
        
        try:
            output_path = await export_all_subdomains()
            status.update(f"[green]Exported all to {output_path}[/green]")
        except Exception as e:
            status.update(f"[red]Export failed: {e}[/red]")

    
    @on(Button.Pressed, "#btn-discover-all")
    async def on_discover_all(self) -> None:
        from agents.discovery import discover_subdomains
        from db.store import get_domain_id
        
        status = self.query_one("#discovery-status")
        tree = self.query_one(DomainTree)
        
        status.update("[yellow]Discovering all domains...[/yellow]")
        
        tasks = []
        for domain in CYBERSECURITY_DOMAINS:
            domain_id = await get_domain_id(domain)
            if not domain_id:
                continue
            
            subdomains = await get_subdomains(domain_id, status="pending")
            for sd in subdomains[:3]:
                tasks.append((domain, sd["id"], sd["name"]))
        
        if not tasks:
            status.update("[green]No pending subdomains found[/green]")
            return
        
        from orchestrator.graph import run_multiple_workers
        app = self.app
        if hasattr(app, "_event_queue") and app._event_queue:
            await run_multiple_workers(tasks, app._event_queue)
            status.update(f"[green]Started {len(tasks)} workers[/green]")
        else:
            status.update("[red]Event queue not initialized[/red]")
    
    @on(Button.Pressed, "#btn-refresh")
    async def on_refresh(self) -> None:
        tree = self.query_one(DomainTree)
        await tree.load_domains()
        status = self.query_one("#discovery-status")
        status.update("[green]Tree refreshed[/green]")


class ExecutionView(Container):
    def compose(self) -> ComposeResult:
        yield Static("Execution View", classes="view-title")
        yield Container(id="workers-container")
        yield Container(id="queue-container")
        yield Horizontal(
            Button("Run Selected", id="btn-run", variant="primary"),
            Button("Run All Checked", id="btn-run-all", variant="warning"),
            Button("Clear Completed", id="btn-clear", variant="default"),
            classes="button-row"
        )
        
    def add_worker_card(self, subdomain: str) -> WorkerCard:
        card = WorkerCard(subdomain)
        container = self.query_one("#workers-container")
        container.mount(card)
        return card


class CybersecApp(App):
    CSS = """
    .view-title {
        text-align: center;
        text-style: bold;
        padding: 1;
        background: $primary;
        color: $text;
    }
    
    .button-row {
        height: auto;
        align: center middle;
        padding: 1;
    }
    
    Button {
        margin: 0 1;
    }
    
    .status-bar {
        padding: 1;
        background: $surface;
        color: $text;
    }
    
    .worker-card {
        margin: 1;
        padding: 1;
        background: $surface;
        border: solid $primary;
    }
    
    .worker-title {
        text-style: bold;
    }
    
    .worker-progress {
        height: 1;
    }
    
    .worker-status {
        color: $text-muted;
    }
    
    #workers-container {
        height: 60%;
        overflow-y: auto;
    }
    
    #queue-container {
        height: 20%;
        overflow-y: auto;
    }
    
    #main-container {
        width: 1fr;
        height: 1fr;
    }
    
    #log-container {
        width: 35;
        height: 1fr;
        display: none;
    }
    
    .status-only {
        padding: 1;
        background: $surface;
        color: $text;
        text-align: center;
    }
    
    .success-status {
        color: $success;
    }
    
    .error-status {
        color: $error;
    }
    
    Screen.is-fullscreen #log-container {
        display: block;
    }
    
    Screen.is-fullscreen #main-container {
        width: 2fr;
    }
    """
    
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("tab", "toggle_view", "Toggle View"),
        ("d", "discover", "Discover"),
        ("r", "refresh", "Refresh"),
        ("l", "toggle_logs", "Toggle Logs"),
        ("s", "stop_all", "Stop All"),
    ]
    
    is_fullscreen: reactive[bool] = reactive(False)
    
    def __init__(self, log_file: str = ""):
        super().__init__()
        self.log_file = log_file
        self.current_view = "discovery"
        self.worker_cards: dict[str, WorkerCard] = {}
        self._event_queue: asyncio.Queue[WorkerEvent] | None = None
        self._shutdown_event: asyncio.Event = asyncio.Event()
        
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="app-container"):
            yield Container(
                DiscoveryView(id="discovery-view"),
                ExecutionView(id="execution-view"),
                id="main-container"
            )
            yield Container(
                LogPanel(self.log_file, id="log-panel"),
                id="log-container"
            )
        yield Footer()
        
    async def on_mount(self) -> None:
        await init_db()
        
        from tools.router import quota_manager
        await quota_manager.initialize()
        
        self._event_queue = create_event_queue()
        self.execution_view = self.query_one("#execution-view")
        self.execution_view.display = False
        
        asyncio.create_task(self._event_consumer())
        asyncio.create_task(self._check_screen_size())
        
    async def on_unmount(self) -> None:
        self._shutdown_event.set()
        await shutdown_db()
        
    async def _check_screen_size(self) -> None:
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(1.0)
                
                size = self.size
                is_full = size.width >= 140
                
                if is_full != self.is_fullscreen:
                    self.is_fullscreen = is_full
                    self._update_log_panel_visibility()
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Screen size check error: {e}")
                
    def _update_log_panel_visibility(self) -> None:
        log_container = self.query_one("#log-container")
        
        if self.is_fullscreen:
            log_container.display = True
        else:
            log_container.display = False
        
    async def _event_consumer(self) -> None:
        while not self._shutdown_event.is_set():
            try:
                event = await asyncio.wait_for(self._event_queue.get(), timeout=1.0)
                self._handle_worker_event(event)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Event consumer error: {e}")
                
    def _handle_worker_event(self, event: WorkerEvent) -> None:
        if event.subdomain not in self.worker_cards:
            card = self.execution_view.add_worker_card(event.subdomain)
            self.worker_cards[event.subdomain] = card
        
        card = self.worker_cards[event.subdomain]
        card.update_progress(event.progress_pct, event.step, event.message)
        
        if event.event_type == "completed":
            logger.info(f"Worker completed for {event.subdomain}")
        elif event.event_type == "failed":
            logger.error(f"Worker failed for {event.subdomain}: {event.message}")
            
    def action_toggle_view(self) -> None:
        discovery = self.query_one("#discovery-view")
        execution = self.query_one("#execution-view")
        
        if self.current_view == "discovery":
            discovery.display = False
            execution.display = True
            self.current_view = "execution"
        else:
            discovery.display = True
            execution.display = False
            self.current_view = "discovery"
            
    def action_toggle_logs(self) -> None:
        log_container = self.query_one("#log-container")
        log_container.display = not log_container.display
        
    def action_stop_all(self) -> None:
        asyncio.create_task(self._stop_all_workers())
    
    async def _stop_all_workers(self) -> None:
        workers_cancelled = self.workers.cancel_all()
        
        from db.store import db
        await db.execute(
            "UPDATE subdomains SET status = 'pending' WHERE status = 'running'"
        )
        await db.commit()
        
        tree = self.query_one(DomainTree)
        await tree.load_domains()
        
        logger.info(f"Stopped {workers_cancelled} workers. All subdomains reset to pending.")
        
    def action_discover(self) -> None:
        logger.debug("Discover action triggered")
        
    def action_refresh(self) -> None:
        asyncio.create_task(self._refresh_tree())
        
    async def _refresh_tree(self) -> None:
        tree = self.query_one(DomainTree)
        await tree.load_domains()
        
    def update_status(self, message: str, is_error: bool = False) -> None:
        self._last_status = message
        self._last_status_is_error = is_error
        
        try:
            status_bar = self.query_one("#discovery-status")
            if self.is_fullscreen:
                status_bar.update(message)
            else:
                prefix = "[red]FAILED[/red]" if is_error else "[green]OK[/green]"
                status_bar.update(f"{prefix} {message[:50]}")
        except Exception:
            pass


async def run_tui(log_file: str) -> None:
    app = CybersecApp(log_file)
    await app.run_async()
