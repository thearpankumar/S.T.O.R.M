import logging
import asyncio
from pathlib import Path
from typing import Any

from textual.widgets import Static
from textual.containers import VerticalScroll
from textual.reactive import reactive


class LogPanel(VerticalScroll):
    DEFAULT_CSS = """
    LogPanel {
        width: 1fr;
        height: 1fr;
        background: $surface;
        border-left: solid $primary;
    }
    
    LogPanel > .log-title {
        text-align: center;
        text-style: bold;
        padding: 0 1;
        background: $primary;
        color: $text;
    }
    
    LogPanel > .log-content {
        padding: 0 1;
        width: 1fr;
        height: auto;
    }
    """
    
    MAX_LINES = 500
    
    log_file: reactive[str] = reactive("")
    
    def __init__(self, log_file: str = "", **kwargs: Any):
        super().__init__(**kwargs)
        self.log_file = log_file
        self._log_content: Static | None = None
        self._position: int = 0
        self._running: bool = False
        self._task: asyncio.Task | None = None
        
    def compose(self):
        yield Static("Logs", classes="log-title")
        self._log_content = Static("", classes="log-content")
        yield self._log_content
        
    async def on_mount(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._tail_log_file())
    
    async def on_unmount(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        
    async def _tail_log_file(self) -> None:
        await asyncio.sleep(0.5)
        
        lines_buffer: list[str] = []
        
        while self._running:
            try:
                if not self.log_file or not Path(self.log_file).exists():
                    await asyncio.sleep(0.5)
                    continue
                
                with open(self.log_file, 'r', encoding='utf-8') as f:
                    f.seek(self._position)
                    new_content = f.read()
                    self._position = f.tell()
                    
                    if new_content:
                        new_lines = new_content.strip().split('\n')
                        for line in new_lines:
                            if line.strip():
                                lines_buffer.append(self._format_line(line))
                        
                        if len(lines_buffer) > self.MAX_LINES:
                            lines_buffer = lines_buffer[-self.MAX_LINES:]
                        
                        if self._log_content:
                            display_text = '\n'.join(lines_buffer[-100:])
                            self._log_content.update(display_text)
                            await self._scroll_to_bottom()
                
                await asyncio.sleep(0.2)
                
            except Exception as e:
                await asyncio.sleep(0.5)
    
    async def _scroll_to_bottom(self) -> None:
        try:
            self.scroll_end(animate=False)
        except Exception:
            pass
    
    def _format_line(self, line: str) -> str:
        line_lower = line.lower()
        
        if 'session start' in line_lower or 'session end' in line_lower:
            return f"[bold cyan]{line}[/bold cyan]"
        elif 'error' in line_lower:
            return f"[red]{line}[/red]"
        elif 'warning' in line_lower:
            return f"[yellow]{line}[/yellow]"
        elif 'debug' in line_lower:
            return f"[dim]{line}[/dim]"
        elif 'info' in line_lower:
            return line
        else:
            return line
    
    def clear(self) -> None:
        self._position = 0
        if self._log_content:
            self._log_content.update("")
