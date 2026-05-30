from dataclasses import dataclass, field
from typing import Callable, Any
from enum import Enum


class ActionType(Enum):
    PRIMARY = "primary"
    SECONDARY = "secondary"
    DESTRUCTIVE = "destructive"
    FUTURE = "future"


@dataclass
class Action:
    id: str
    icon: str
    label: str
    handler: Callable[..., Any]
    action_type: ActionType = ActionType.PRIMARY
    shortcut: str | None = None
    requires_selection: bool = True
    allowed_statuses: list[str] | None = None
    position: int = 100
    confirm: bool = False
    confirm_message: str = ""
    disabled: bool = False

    def is_available_for_status(self, status: str) -> bool:
        if self.allowed_statuses is None:
            return True
        return status in self.allowed_statuses

    def is_available_for_selection(self, selection_count: int) -> bool:
        if not self.requires_selection:
            return True
        return selection_count > 0

    def get_icon_svg(self, size: int = 14) -> str:
        from tui.utils.icons import icon_html, status_icon
        
        icon_map = {
            "pending": lambda: status_icon("pending", size),
            "running": lambda: status_icon("running", size),
            "done": lambda: status_icon("done", size),
            "failed": lambda: status_icon("failed", size),
            "search": lambda: icon_html("search", "actions", size),
            "play": lambda: icon_html("play", "actions", size),
            "stop": lambda: icon_html("stop", "actions", size),
            "chart": lambda: icon_html("chart", "actions", size),
            "trash": lambda: icon_html("trash", "actions", size),
            "refresh": lambda: icon_html("refresh", "actions", size),
            "export": lambda: icon_html("export", "actions", size),
            "file": lambda: icon_html("file", "ui", size),
            "clipboard": lambda: icon_html("clipboard", "actions", size),
            "clock": lambda: icon_html("clock", "ui", size),
            "info": lambda: icon_html("info", "ui", size),
        }
        
        return icon_map.get(self.icon, lambda: "")()


@dataclass
class ActionRegistry:
    actions: dict[str, Action] = field(default_factory=dict)

    def register(self, action: Action) -> None:
        self.actions[action.id] = action

    def get(self, action_id: str) -> Action | None:
        return self.actions.get(action_id)

    def get_all(self) -> list[Action]:
        return sorted(self.actions.values(), key=lambda a: a.position)

    def get_for_status(self, status: str) -> list[Action]:
        return sorted(
            [a for a in self.actions.values() if a.is_available_for_status(status)],
            key=lambda a: a.position,
        )

    def get_for_selection(self, selection_count: int) -> list[Action]:
        return sorted(
            [a for a in self.actions.values() if a.is_available_for_selection(selection_count)],
            key=lambda a: a.position,
        )

    def get_primary_actions(self) -> list[Action]:
        return [a for a in self.get_all() if a.action_type == ActionType.PRIMARY]

    def get_secondary_actions(self) -> list[Action]:
        return [a for a in self.get_all() if a.action_type == ActionType.SECONDARY]

    def get_future_actions(self) -> list[Action]:
        return [a for a in self.get_all() if a.action_type == ActionType.FUTURE]


registry = ActionRegistry()


def register_action(action: Action) -> None:
    registry.register(action)


def get_actions_for_context(
    status: str | None = None,
    selection_count: int = 0,
    action_types: list[ActionType] | None = None,
) -> list[Action]:
    actions = registry.get_all()

    if status is not None:
        actions = [a for a in actions if a.is_available_for_status(status)]

    actions = [a for a in actions if a.is_available_for_selection(selection_count)]

    if action_types:
        actions = [a for a in actions if a.action_type in action_types]

    return sorted(actions, key=lambda a: a.position)
