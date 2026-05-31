from typing import Any
import streamlit as st

from tui.utils.icons import status_icon, icon_html, sanitize_for_html


def get_status_icon(status: str) -> str:
    return status_icon(status, size=14)


def get_status_color(status: str) -> str:
    return {"pending": "gray", "running": "blue", "done": "green", "failed": "red"}.get(status, "gray")


def get_status_emoji(status: str) -> str:
    return status_icon(status, size=14)


STATUS_CHAR = {
    "pending": status_icon("pending", 12),
    "running": status_icon("running", 12),
    "done": status_icon("done", 12),
    "failed": status_icon("failed", 12),
}
STATUS_COLOR_HEX = {
    "pending": "#6b7280",
    "running": "#3b82f6",
    "done": "#22c55e",
    "failed": "#ef4444",
}


def get_status_label_html(status: str) -> str:
    svg = status_icon(status, 12)
    color = STATUS_COLOR_HEX.get(status, "#6b7280")
    bg = {"pending": "#1f2937", "running": "#1e3a5f", "done": "#14532d", "failed": "#7f1d1d"}.get(status, "#1f2937")
    return (
        f"<span style='background:{bg}; color:{color}; border:1px solid {color}40;"
        f"border-radius:10px; padding:1px 8px; font-size:11px;"
        f"font-weight:600; letter-spacing:0.3px; white-space:nowrap;'>{svg} {status}</span>"
    )


def init_tree_state() -> None:
    defaults: dict[str, Any] = {
        "expanded_domains": {},
        "selected_items": set(),
        "detail_item": None,
        "search_query": "",
        "context_menu_target": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value.copy() if isinstance(value, set) else value


def toggle_domain(domain: str) -> None:
    expanded = st.session_state.get("expanded_domains", {})
    expanded[domain] = not expanded.get(domain, False)
    st.session_state.expanded_domains = expanded


def is_domain_expanded(domain: str) -> bool:
    return st.session_state.get("expanded_domains", {}).get(domain, False)


def toggle_selection(item_key: str) -> None:
    selected = st.session_state.get("selected_items", set())
    if isinstance(selected, list):
        selected = set(selected)
    if item_key in selected:
        selected.discard(item_key)
    else:
        selected.add(item_key)
    st.session_state.selected_items = selected


def select_all(domain: str, subdomains: list[dict]) -> None:
    selected = st.session_state.get("selected_items", set())
    if isinstance(selected, list):
        selected = set(selected)
    for sd in subdomains:
        selected.add(f"{domain}_{sd['id']}")
    st.session_state.selected_items = selected


def deselect_all() -> None:
    st.session_state.selected_items = set()


def get_selection_count() -> int:
    return len(st.session_state.get("selected_items", set()))


def set_detail_item(item_key: str | None) -> None:
    st.session_state.detail_item = item_key


def get_detail_item() -> str | None:
    return st.session_state.get("detail_item")


def filter_items(items: list[dict], query: str) -> list[dict]:
    if not query:
        return items
    q = query.lower()
    return [i for i in items if q in i.get("name", "").lower()]


def render_tree_header(tree_data: dict[str, list[dict]]) -> str:
    col_input, col_clear = st.columns([6, 1], gap="small")
    with col_input:
        search_query = st.text_input(
            "search",
            placeholder="Filter...",
            key="tree_search_input",
            label_visibility="collapsed",
        )
    with col_clear:
        st.markdown("<div style='margin-top:4px;'>", unsafe_allow_html=True)
        if search_query and st.button("X", key="tree_search_clear", help="Clear search"):
            st.session_state["tree_search_input"] = ""
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

    st.session_state.search_query = search_query

    if search_query:
        q = search_query.lower()
        total_matches = sum(
            (1 if q in domain.lower() else 0)
            + sum(1 for sd in sds if q in sd.get("name", "").lower())
            for domain, sds in tree_data.items()
        )
        st.caption(f"{total_matches} match{'es' if total_matches != 1 else ''}")

    return search_query


def _count_badge(subdomains: list[dict]) -> str:
    done = sum(1 for s in subdomains if s.get("status") == "done")
    running = sum(1 for s in subdomains if s.get("status") == "running")
    total = len(subdomains)
    if running > 0:
        color, bg = "#3b82f6", "#1e3a5f"
        icon = status_icon("running", 12)
        text = f"{running} running / {done}/{total} done"
    elif done == total and total > 0:
        color, bg = "#22c55e", "#14532d"
        icon = status_icon("done", 12)
        text = f"{done}/{total}"
    else:
        color, bg = "#6b7280", "transparent"
        icon = status_icon("pending", 12)
        text = f"{done}/{total}"
    return (
        f"<span style='color:{color}; background:{bg}; border:1px solid {color}40;"
        f"border-radius:8px; padding:1px 7px; font-size:11px; font-weight:600;"
        f"white-space:nowrap; font-family:monospace;'>{icon} {text}</span>"
    )


def render_domain_row(domain: str, subdomains: list[dict], is_expanded: bool) -> None:
    is_active = st.session_state.get("detail_item") == f"domain_{domain}"
    active_dot = "• " if is_active else ""
    label = f"{active_dot}{domain}"
    mat_icon = ":material/folder_open:" if is_expanded else ":material/folder:"

    col_btn, col_badge = st.columns([5, 1.2], gap="small")

    with col_btn:
        if st.button(label, key=f"click_domain_{domain}", width="stretch", type="tertiary", icon=mat_icon):
            set_detail_item(f"domain_{domain}")
            toggle_domain(domain)
            st.rerun()

    with col_badge:
        st.markdown(
            f"<div style='display:flex; align-items:center; height:100%; padding:2px 0;'>"
            f"{_count_badge(subdomains)}</div>",
            unsafe_allow_html=True,
        )


def render_subdomain_rows(domain: str, subdomains: list[dict], search_query: str = "") -> None:
    filtered = filter_items(subdomains, search_query) if search_query else subdomains

    if not filtered:
        st.markdown(
            "<p style='color:#475569; font-size:12px; margin:2px 0 2px 32px;'>"
            "No matches</p>",
            unsafe_allow_html=True,
        )
        return

    last_idx = len(filtered) - 1

    for i, sd in enumerate(filtered):
        status = sd.get("status", "pending")
        item_key = f"{domain}_{sd['id']}"
        is_selected = item_key in st.session_state.get("selected_items", set())
        is_active = st.session_state.get("detail_item") == f"subdomain_{item_key}"

        status_mat = {
            "pending": ":material/schedule:",
            "running": ":material/sync:",
            "done": ":material/check_circle:",
            "failed": ":material/error:",
        }.get(status, ":material/help:")
        marker = "› " if is_active else ""
        safe_sd_name = sanitize_for_html(sd["name"])
        label = f"{marker}{safe_sd_name}"

        col_chk, col_btn = st.columns([0.18, 5.82], gap="small")

        with col_chk:
            checked = st.checkbox(
                " ", value=is_selected,
                key=f"check_{item_key}",
                label_visibility="collapsed",
            )
            if checked != is_selected:
                toggle_selection(item_key)
                st.rerun()

        with col_btn:
            if st.button(label, key=f"click_{item_key}", width="stretch", type="tertiary", icon=status_mat):
                set_detail_item(f"subdomain_{item_key}")
                st.rerun()


def render_domain_tree(
    tree_data: dict[str, list[dict]],
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    init_tree_state()

    search_query = render_tree_header(tree_data)

    sel_count = get_selection_count()
    if sel_count > 0:
        st.markdown(
            f"<p style='font-size:12px; color:#3b82f6; font-weight:600; margin:2px 0;'>"
            f"[{sel_count} selected]</p>",
            unsafe_allow_html=True,
        )

    st.markdown(
        "<hr style='border:none; border-top:1px solid #1e2d3d; margin:4px 0 6px;'>",
        unsafe_allow_html=True,
    )

    with st.container(height=520, border=False):
        for domain, subdomains in tree_data.items():
            is_expanded = is_domain_expanded(domain)

            domain_match = not search_query or search_query.lower() in domain.lower()
            subdomain_match = any(
                search_query.lower() in sd.get("name", "").lower() for sd in subdomains
            ) if search_query else False

            if search_query and not (domain_match or subdomain_match):
                continue

            render_domain_row(domain, subdomains, is_expanded)

            if is_expanded or (search_query and subdomain_match):
                st.markdown(
                    "<div style='border-left:1px solid #1e3a5f; margin-left:10px; "
                    "padding:0; margin-top:0; margin-bottom:0;'>",
                    unsafe_allow_html=True,
                )
                render_subdomain_rows(domain, subdomains, search_query)
                st.markdown("</div>", unsafe_allow_html=True)

    return None, None, st.session_state.get("context_menu_target")
