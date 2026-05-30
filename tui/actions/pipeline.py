import streamlit as st

from tui.actions.registry import Action, ActionType, register_action, get_actions_for_context


def discover_subdomains_handler(domain: str, **kwargs):
    st.session_state._pending_action = ("discover", domain)


def run_pipeline_handler(domain: str, subdomain_id: int, subdomain_name: str, **kwargs):
    st.session_state._pending_action = ("run_pipeline", domain, subdomain_id, subdomain_name)


def stop_pipeline_handler(subdomain_id: int, **kwargs):
    st.session_state._pending_action = ("stop_pipeline", subdomain_id)


def export_handler(subdomain_id: int, subdomain_name: str, **kwargs):
    st.session_state._pending_action = ("export", subdomain_id, subdomain_name)


def export_all_handler(**kwargs):
    st.session_state._pending_action = ("export_all",)


def clear_data_handler(subdomain_id: int, **kwargs):
    st.session_state._pending_action = ("clear_data", subdomain_id)


def view_logs_handler(subdomain_name: str, **kwargs):
    st.session_state._pending_action = ("view_logs", subdomain_name)


def copy_name_handler(name: str, **kwargs):
    st.session_state._pending_action = ("copy_name", name)


def bulk_run_handler(selections: list, **kwargs):
    st.session_state._pending_action = ("bulk_run", selections)


def bulk_export_handler(selections: list, **kwargs):
    st.session_state._pending_action = ("bulk_export", selections)


def bulk_clear_handler(selections: list, **kwargs):
    st.session_state._pending_action = ("bulk_clear", selections)


def stop_all_handler(**kwargs):
    st.session_state._pending_action = ("stop_all",)


def refresh_handler(**kwargs):
    st.session_state._pending_action = ("refresh",)


def register_all_actions():
    register_action(Action(
        id="discover_subdomains",
        icon="search",
        label="Discover Subdomains",
        handler=discover_subdomains_handler,
        action_type=ActionType.PRIMARY,
        position=5,
        requires_selection=False,
    ))

    register_action(Action(
        id="run_pipeline",
        icon="play",
        label="Run Pipeline",
        handler=run_pipeline_handler,
        action_type=ActionType.PRIMARY,
        shortcut="Ctrl+R",
        allowed_statuses=["pending"],
        position=10,
    ))

    register_action(Action(
        id="stop_pipeline",
        icon="stop",
        label="Stop Pipeline",
        handler=stop_pipeline_handler,
        action_type=ActionType.PRIMARY,
        shortcut="Ctrl+S",
        allowed_statuses=["running"],
        position=15,
    ))

    register_action(Action(
        id="export",
        icon="chart",
        label="Export",
        handler=export_handler,
        action_type=ActionType.PRIMARY,
        shortcut="Ctrl+E",
        allowed_statuses=["done"],
        position=20,
    ))

    register_action(Action(
        id="export_all",
        icon="chart",
        label="Export All Completed",
        handler=export_all_handler,
        action_type=ActionType.PRIMARY,
        position=25,
        requires_selection=False,
    ))

    register_action(Action(
        id="clear_data",
        icon="trash",
        label="Clear Data",
        handler=clear_data_handler,
        action_type=ActionType.DESTRUCTIVE,
        position=30,
        confirm=True,
        confirm_message="This will delete all data for this subdomain. Continue?",
    ))

    register_action(Action(
        id="view_logs",
        icon="file",
        label="View Logs",
        handler=view_logs_handler,
        action_type=ActionType.SECONDARY,
        shortcut="Ctrl+L",
        position=40,
        requires_selection=False,
    ))

    register_action(Action(
        id="copy_name",
        icon="clipboard",
        label="Copy Name",
        handler=copy_name_handler,
        action_type=ActionType.SECONDARY,
        shortcut="Ctrl+C",
        position=45,
        requires_selection=False,
    ))

    register_action(Action(
        id="bulk_run",
        icon="play",
        label="Run Selected",
        handler=bulk_run_handler,
        action_type=ActionType.PRIMARY,
        position=50,
    ))

    register_action(Action(
        id="bulk_export",
        icon="chart",
        label="Export Selected",
        handler=bulk_export_handler,
        action_type=ActionType.PRIMARY,
        position=55,
    ))

    register_action(Action(
        id="bulk_clear",
        icon="trash",
        label="Clear Selected",
        handler=bulk_clear_handler,
        action_type=ActionType.DESTRUCTIVE,
        position=60,
        confirm=True,
        confirm_message="This will clear all selected subdomains. Continue?",
    ))

    register_action(Action(
        id="stop_all",
        icon="stop",
        label="Stop All",
        handler=stop_all_handler,
        action_type=ActionType.PRIMARY,
        position=70,
        requires_selection=False,
    ))

    register_action(Action(
        id="refresh",
        icon="refresh",
        label="Refresh",
        handler=refresh_handler,
        action_type=ActionType.SECONDARY,
        position=80,
        requires_selection=False,
    ))

    register_action(Action(
        id="schedule_run",
        icon="clock",
        label="Schedule Run",
        handler=lambda **kwargs: None,
        action_type=ActionType.FUTURE,
        position=100,
        disabled=True,
    ))

    register_action(Action(
        id="compare_tools",
        icon="chart",
        label="Compare Tools",
        handler=lambda **kwargs: None,
        action_type=ActionType.FUTURE,
        position=105,
        disabled=True,
    ))

    register_action(Action(
        id="export_report",
        icon="export",
        label="Export Report",
        handler=lambda **kwargs: None,
        action_type=ActionType.FUTURE,
        position=110,
        disabled=True,
    ))
