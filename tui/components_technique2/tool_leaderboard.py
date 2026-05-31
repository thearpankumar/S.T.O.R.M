"""
Technique 2: Tool Leaderboard Component

Displays ranked tools with scores and factor breakdown.
"""

import streamlit as st

from tui.utils.icons import icon_html


def render_tool_leaderboard(
    tools_enterprise: list[dict],
    tools_opensource: list[dict],
    selected_tool: str | None = None,
) -> tuple[str | None, dict | None]:
    """
    Render ranked tools list with scores.
    
    Returns:
        (selected_tool_name, selected_tool_data)
    """
    
    selected = None
    selected_data = None
    
    st.markdown("**Enterprise Tools (Top 15)**")
    
    for tool in tools_enterprise[:5]:
        rank = tool.get("rank_position", 0)
        name = tool.get("product_name", "")
        score = tool.get("composite_score", 0)
        
        is_selected = selected_tool == name
        
        col1, col2, col3 = st.columns([0.5, 3, 1])
        
        with col1:
            st.markdown(f"**#{rank}**")
        
        with col2:
            if st.button(
                name,
                key=f"t2_tool_ent_{name}",
                width="stretch",
            ):
                selected = name
                selected_data = tool
        
        with col3:
            color = "#22c55e" if score >= 70 else "#f59e0b" if score >= 50 else "#ef4444"
            st.markdown(
                f"<span style='color:{color}; font-weight:600;'>{score:.1f}</span>",
                unsafe_allow_html=True,
            )
    
    if len(tools_enterprise) > 5:
        with st.expander(f"Show all {len(tools_enterprise)} enterprise tools"):
            for tool in tools_enterprise[5:]:
                name = tool.get("product_name", "")
                score = tool.get("composite_score", 0)
                rank = tool.get("rank_position", 0)
                color = "#22c55e" if score >= 70 else "#f59e0b" if score >= 50 else "#ef4444"
                st.markdown(
                    f"#{rank} **{name}** — <span style='color:{color};'>{score:.1f}</span>",
                    unsafe_allow_html=True,
                )
    
    st.markdown("---")
    st.markdown("**Open-Source Tools (Top 5)**")
    
    for tool in tools_opensource[:5]:
        rank = tool.get("rank_position", 0)
        name = tool.get("product_name", "")
        score = tool.get("composite_score", 0)
        
        col1, col2, col3 = st.columns([0.5, 3, 1])
        
        with col1:
            st.markdown(f"**#{rank}**")
        
        with col2:
            if st.button(
                name,
                key=f"t2_tool_oss_{name}",
                width="stretch",
            ):
                selected = name
                selected_data = tool
        
        with col3:
            color = "#22c55e" if score >= 70 else "#f59e0b" if score >= 50 else "#ef4444"
            st.markdown(
                f"<span style='color:{color}; font-weight:600;'>{score:.1f}</span>",
                unsafe_allow_html=True,
            )
    
    return selected, selected_data


def render_score_breakdown(tool: dict) -> None:
    """Render the ranking factor breakdown for a tool."""
    
    if not tool:
        st.info("Select a tool to see score breakdown")
        return
    
    name = tool.get("product_name", "Unknown")
    score = tool.get("composite_score", 0)
    
    st.markdown(f"**Score Breakdown: {name}**")
    
    factors = [
        ("Subdomain Presence", tool.get("subdomain_presence_score", 0), 0.40),
        ("Feature Coverage", tool.get("feature_coverage_score", 0), 0.20),
        ("Market Presence", tool.get("market_presence_score", 0), 0.20),
        ("Rank Distribution", tool.get("rank_distribution_score", 0), 0.20),
    ]
    
    for factor_name, value, weight in factors:
        pct = value * 100
        contribution = value * weight * 100
        
        bar_value = int(pct / 100 * 100)
        
        st.markdown(
            f"<div style='margin-bottom:4px;'><small>{factor_name} ({weight*100:.0f}%): {pct:.0f}% = {contribution:.1f} pts</small></div>",
            unsafe_allow_html=True,
        )
        st.progress(bar_value / 100)
    
    st.markdown("---")
    color = "#22c55e" if score >= 70 else "#f59e0b" if score >= 50 else "#ef4444"
    st.markdown(
        f"<div style='font-size:16px; font-weight:600;'>COMPOSITE: <span style='color:{color};'>{score:.1f}</span> / 100</div>",
        unsafe_allow_html=True,
    )
