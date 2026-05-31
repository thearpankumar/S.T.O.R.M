"""
Technique 2: Domain Tree Component

Displays list of domains with rankings status and action buttons.
"""

import streamlit as st

from config.domains import CYBERSECURITY_DOMAINS
from tui.utils.icons import icon_html, status_icon


def render_domain_tree_t2(
    rankings: list[dict],
    on_run: callable,
    selected_domain: str | None = None,
) -> str | None:
    """
    Render domain list with ranking status.
    
    Args:
        rankings: List of domain ranking dicts
        on_run: Callback(domain_name) when Run button clicked
        selected_domain: Currently selected domain
    
    Returns:
        Selected domain name (or None)
    """
    
    rankings_by_domain = {r.get("domain_name"): r for r in rankings}
    
    for domain in CYBERSECURITY_DOMAINS:
        ranking = rankings_by_domain.get(domain)
        status = ranking.get("status", "pending") if ranking else "pending"
        
        ent_count = ranking.get("selected_enterprise_tools", 0) if ranking else 0
        oss_count = ranking.get("selected_opensource_tools", 0) if ranking else 0
        
        is_selected = selected_domain == domain
        is_running = status == "running"
        
        col1, col2, col3 = st.columns([3, 1.5, 1])
        
        with col1:
            status_ico = status_icon(status, 14)
            selected_marker = "▶ " if is_selected else ""
            
            btn_label = f"{selected_marker}{domain}"
            
            if st.button(
                btn_label,
                key=f"t2_domain_{domain}",
                width="stretch",
                disabled=is_running,
            ):
                st.session_state.t2_selected_domain = domain
                st.rerun()
        
        with col2:
            count_text = f"{ent_count}+{oss_count}" if status == "done" else "—"
            color = "#22c55e" if status == "done" else "#94a3b8"
            st.markdown(
                f"<span style='color:{color}; font-size:12px;'>{status_ico} {count_text}</span>",
                unsafe_allow_html=True,
            )
        
        with col3:
            if is_running:
                st.button("...", key=f"t2_run_{domain}", disabled=True, width="stretch")
            else:
                btn_label = "Rerun" if status == "done" else "Run"
                if st.button(btn_label, key=f"t2_run_{domain}", width="stretch"):
                    on_run(domain)
    
    return st.session_state.get("t2_selected_domain")
