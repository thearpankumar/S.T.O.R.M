import streamlit as st

from tui.actions.registry import Action, ActionType, get_actions_for_context
from tui.components.domain_tree import get_status_icon
from tui.utils.icons import icon_html


def render_context_menu(
    target: str,
    status: str = None,
    selection_count: int = 0,
    on_action: callable = None,
) -> Action | None:
    if target.startswith("domain_"):
        actions = get_actions_for_context(
            status=None,
            selection_count=0,
            action_types=[ActionType.PRIMARY, ActionType.SECONDARY],
        )
    else:
        actions = get_actions_for_context(
            status=status,
            selection_count=0,
            action_types=[ActionType.PRIMARY, ActionType.SECONDARY],
        )

    primary_actions = [a for a in actions if a.action_type == ActionType.PRIMARY]
    secondary_actions = [a for a in actions if a.action_type == ActionType.SECONDARY]
    destructive_actions = [a for a in actions if a.action_type == ActionType.DESTRUCTIVE]

    selected_action = None

    with st.container():
        st.markdown("**Actions:**")

        if primary_actions:
            cols = st.columns(len(primary_actions))
            for i, action in enumerate(primary_actions):
                with cols[i]:
                    icon_svg = action.get_icon_svg(14)
                    if st.button(
                        f"{icon_svg} {action.label}",
                        key=f"ctx_{action.id}_{target}",
                        disabled=action.disabled,
                        width="stretch",
                    ):
                        selected_action = action

        st.markdown("---")

        if secondary_actions:
            for action in secondary_actions:
                icon_svg = action.get_icon_svg(14)
                if st.button(
                    f"{icon_svg} {action.label}",
                    key=f"ctx_{action.id}_{target}",
                    disabled=action.disabled,
                    width="stretch",
                ):
                    selected_action = action

        if destructive_actions:
            st.markdown("---")
            st.markdown("**Danger Zone:**")
            for action in destructive_actions:
                icon_svg = action.get_icon_svg(14)
                if st.button(
                    f"{icon_svg} {action.label}",
                    key=f"ctx_{action.id}_{target}",
                    disabled=action.disabled,
                    width="stretch",
                ):
                    if action.confirm:
                        st.session_state._confirm_action = action.id
                        st.session_state._confirm_target = target
                    else:
                        selected_action = action

    return selected_action


def render_action_confirmation(
    action: Action,
    target: str,
    on_confirm: callable = None,
    on_cancel: callable = None,
) -> bool:
    st.warning(action.confirm_message)

    check_svg = icon_html("check", "ui", 14)
    close_svg = icon_html("close", "ui", 14)
    col1, col2 = st.columns(2)
    with col1:
        if st.button(f"{check_svg} Confirm", width="stretch", type="primary"):
            if on_confirm:
                on_confirm()
            return True
    with col2:
        if st.button(f"{close_svg} Cancel", width="stretch"):
            if on_cancel:
                on_cancel()
            return False

    return False


def render_floating_menu(
    target: str,
    status: str = None,
) -> Action | None:
    st.markdown("""
    <style>
    .context-menu {
        background: white;
        border-radius: 8px;
        box-shadow: 0 4px 12px rgba(0,0,0,0.15);
        padding: 8px;
        min-width: 180px;
    }
    </style>
    """, unsafe_allow_html=True)

    status_icon_svg = get_status_icon(status) if status else ""

    if target.startswith("domain_"):
        domain_name = target.replace("domain_", "")
        folder_svg = icon_html("folder", "navigation", 14)
        st.markdown(f"**{folder_svg} {domain_name}**")
    else:
        st.markdown(f"**{status_icon_svg} Item Actions**")

    st.markdown("---")

    actions = get_actions_for_context(
        status=status,
        selection_count=0,
    )

    for action in actions:
        if action.action_type == ActionType.FUTURE:
            continue

        icon_svg = action.get_icon_svg(14)

        if st.button(
            f"{icon_svg} {action.label}",
            key=f"float_{action.id}_{target}",
            disabled=action.disabled,
            width="stretch",
        ):
            return action

    return None


def clear_context_menu() -> None:
    st.session_state.context_menu_target = None
