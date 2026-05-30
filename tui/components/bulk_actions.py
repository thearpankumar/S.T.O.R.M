import streamlit as st

from tui.components.domain_tree import deselect_all
from tui.utils.icons import icon_html


def render_bulk_action_bar(
    selection_count: int,
    search_query: str = "",
) -> None:
    st.markdown(
        "<hr style='border-color:#1e2533; margin:12px 0 8px;'>",
        unsafe_allow_html=True,
    )

    if selection_count == 0:
        checkbox_svg = icon_html("checkbox", "ui", 14)
        st.markdown(
            f"<p style='font-size:12px; color:#334155; text-align:center; margin:4px 0;'>"
            f"{checkbox_svg} Use checkboxes in the tree to select items for bulk operations"
            f"</p>",
            unsafe_allow_html=True,
        )
        return

    checked_svg = icon_html("checkbox-checked", "ui", 14)
    st.markdown(
        f"<div style='background:rgba(59,130,246,0.10); border:1px solid rgba(59,130,246,0.25);"
        f"border-radius:10px; padding:10px 16px; display:flex; align-items:center; gap:12px;"
        f"margin-bottom:6px;'>"
        f"<span style='color:#3b82f6; font-weight:700; font-size:13px; white-space:nowrap;'>"
        f"{checked_svg} {selection_count} selected</span>"
        f"</div>",
        unsafe_allow_html=True,
    )

    col1, col2, col3, col4 = st.columns(4)
    play_svg = icon_html("play", "actions", 14)
    chart_svg = icon_html("chart", "actions", 14)
    trash_svg = icon_html("trash", "actions", 14)
    close_svg = icon_html("close", "ui", 14)

    with col1:
        if st.button(
            f"{play_svg} Run Selected", key="bulk_run_selected",
            use_container_width=True,
        ):
            st.session_state._bulk_action = "bulk_run"

    with col2:
        if st.button(
            f"{chart_svg} Export Selected", key="bulk_export_selected",
            use_container_width=True,
        ):
            st.session_state._bulk_action = "bulk_export"

    with col3:
        if st.button(
            f"{trash_svg} Clear Selected", key="bulk_clear_selected",
            use_container_width=True,
        ):
            st.session_state._bulk_action = "bulk_clear"

    with col4:
        if st.button(
            f"{close_svg} Clear Selection", key="bulk_deselect",
            use_container_width=True,
        ):
            deselect_all()
            st.rerun()
