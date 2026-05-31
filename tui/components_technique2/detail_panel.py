"""
Technique 2: Detail Panel Component

Shows domain summary, top tools, and actions.
"""

import streamlit as st

from tui.utils.icons import icon_html
from tui.components_technique2.tool_leaderboard import render_tool_leaderboard, render_score_breakdown


def render_detail_panel_t2(
    domain_name: str,
    ranking: dict | None,
    tools_enterprise: list[dict],
    tools_opensource: list[dict],
    is_running: bool,
    pipeline_info: dict | None = None,
    on_run: callable = None,
    on_export: callable = None,
):
    """
    Render the detail panel for a selected domain.
    """
    
    if not domain_name:
        st.info("Select a domain from the list to view details")
        return
    
    st.markdown(f"### {domain_name}")
    st.markdown("---")
    
    if ranking:
        st.markdown("**Aggregation Summary**")
        
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric(
                "Enterprise",
                f"{ranking.get('total_enterprise_tools', 0)}→{ranking.get('selected_enterprise_tools', 0)}",
            )
        with col2:
            st.metric(
                "Open-Source",
                f"{ranking.get('total_opensource_tools', 0)}→{ranking.get('selected_opensource_tools', 0)}",
            )
        with col3:
            st.metric("Status", ranking.get("status", "pending"))
        
        st.markdown("---")
    
    if is_running and pipeline_info:
        st.markdown("**Pipeline Progress**")
        progress = pipeline_info.get("progress", 0.0)
        step = pipeline_info.get("step", "d1")
        message = pipeline_info.get("message", "")
        
        st.progress(progress)
        st.caption(f"Stage: {step.upper()} — {message}")
        
        stages = [
            ("D1", "Tools", progress > 0.20),
            ("D2", "Features", progress > 0.35),
            ("D3", "Subfeatures", progress > 0.55),
            ("D4", "Matrix", progress > 0.85),
            ("D5", "Export", progress >= 1.0),
        ]
        
        cols = st.columns(5)
        for i, (sl, slabel, done) in enumerate(stages):
            with cols[i]:
                icon = "✅" if done else "⏳"
                st.markdown(f"<div style='text-align:center;'>{icon}<br/><small>{sl}</small></div>", unsafe_allow_html=True)
        
        st.markdown("---")
    
    if tools_enterprise or tools_opensource:
        st.markdown("**Top Ranked Tools**")
        
        if "t2_selected_tool" not in st.session_state:
            st.session_state.t2_selected_tool = None
        
        selected_tool, selected_data = render_tool_leaderboard(
            tools_enterprise,
            tools_opensource,
            st.session_state.t2_selected_tool,
        )
        
        if selected_tool:
            st.session_state.t2_selected_tool = selected_tool
        
        st.markdown("---")
        
        st.markdown("**Score Breakdown**")
        render_score_breakdown(selected_data if st.session_state.t2_selected_tool else (tools_enterprise[0] if tools_enterprise else None))
        
        st.markdown("---")
    
    col1, col2, col3 = st.columns(3)
    
    with col1:
        if st.button(
            "Run Ranking" if not is_running else "Running...",
            key=f"t2_action_run_{domain_name}",
            width="stretch",
            disabled=is_running,
            type="primary",
        ):
            if on_run:
                on_run(domain_name)
    
    with col2:
        has_data = ranking and ranking.get("status") == "done"
        if st.button(
            "Export",
            key=f"t2_action_export_{domain_name}",
            width="stretch",
            disabled=not has_data,
        ):
            if on_export:
                on_export(domain_name)
    
    with col3:
        has_data = ranking and ranking.get("status") == "done"
        if st.button(
            "View Matrix",
            key=f"t2_action_matrix_{domain_name}",
            width="stretch",
            disabled=not has_data,
        ):
            st.session_state.t2_show_matrix = domain_name
