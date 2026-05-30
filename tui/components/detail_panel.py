from typing import Any, Callable
import streamlit as st

from tui.utils.icons import status_icon, icon_html, sanitize_for_html


def render_empty_state() -> None:
    folder_svg = icon_html("folder", "navigation", 48)
    st.markdown(f"""
    <div style='text-align:center; padding:60px 20px; opacity:0.7;'>
        <div style='font-size:52px; margin-bottom:16px;'>{folder_svg}</div>
        <h3 style='color:#94a3b8; margin-bottom:8px;'>Nothing selected</h3>
        <p style='color:#64748b; font-size:13px; line-height:1.6;'>
            Click a <strong>domain</strong> to view its stats and bulk actions.<br/>
            Click a <strong>subdomain</strong> to see details and run operations.
        </p>
    </div>
    """, unsafe_allow_html=True)

    st.markdown(
        "<hr style='border-color:#1e2533; margin:8px 0 12px;'>",
        unsafe_allow_html=True,
    )
    info_svg = icon_html("info", "ui", 14)
    chev_r = icon_html("chevron-right", "navigation", 12)
    chev_d = icon_html("chevron-down", "navigation", 12)
    st.markdown(
        f"<p style='color:#475569; font-size:12px; text-align:center;'>"
        f"{info_svg} Use checkboxes for bulk operations / Click {chev_r}/{chev_d} to expand domains"
        f"</p>",
        unsafe_allow_html=True,
    )


def _action_btn(label: str, key: str, enabled: bool, help_text: str = "", icon: str = None) -> bool:
    kwargs = {
        "label": label,
        "key": key,
        "disabled": not enabled,
        "use_container_width": True,
    }
    if help_text and not enabled:
        kwargs["help"] = help_text
    if icon:
        kwargs["icon"] = icon

    return st.button(**kwargs)


def _section_header(text: str) -> None:
    st.markdown(
        f"<p style='font-size:12px; font-weight:700; letter-spacing:1.5px;"
        f"color:#64748b; text-transform:uppercase; text-align:left;"
        f"margin:4px 0 12px;'>{text}</p>",
        unsafe_allow_html=True,
    )


def render_domain_detail(
    domain: str,
    subdomains: list[dict],
    on_discover: Callable | None = None,
    on_run_all: Callable | None = None,
    on_export_all: Callable | None = None,
) -> None:
    done_count = sum(1 for s in subdomains if s.get("status") == "done")
    running_count = sum(1 for s in subdomains if s.get("status") == "running")
    pending_count = sum(1 for s in subdomains if s.get("status") == "pending")
    failed_count = sum(1 for s in subdomains if s.get("status") == "failed")
    total_count = len(subdomains)

    safe_domain = sanitize_for_html(domain)
    st.markdown(
        f"<h3 style='text-align:left; margin:0 0 4px; font-size:22px;'>{safe_domain}</h3>",
        unsafe_allow_html=True,
    )
    st.markdown(
        "<hr style='border-color:#1e2533; margin:12px 0 24px;'>",
        unsafe_allow_html=True,
    )

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total", total_count)
    c2.metric(":material/check_circle: Done", done_count)
    c3.metric(":material/sync: Running", running_count)
    c4.metric(":material/schedule: Pending", pending_count)
    c5.metric(":material/error: Failed", failed_count)

    st.markdown(
        "<hr style='border-color:#1e2533; margin:28px 0;'>",
        unsafe_allow_html=True,
    )

    if subdomains:
        with st.expander("Subdomain list", expanded=False, icon=":material/list:"):
            for sd in subdomains[:15]:
                status = sd.get("status", "pending")
                svg = status_icon(status, 12)
                safe_name = sanitize_for_html(sd["name"])
                st.markdown(f"{svg} {safe_name}", unsafe_allow_html=True)
            if len(subdomains) > 15:
                st.caption(f"... and {len(subdomains) - 15} more")

    st.markdown(
        "<hr style='border-color:#1e2533; margin:28px 0;'>",
        unsafe_allow_html=True,
    )

    _section_header("Domain Actions")
    col1, col2, col3 = st.columns(3)

    with col1:
        if _action_btn("Discover Subdomains", "dom_discover", True, icon=":material/search:"):
            if on_discover:
                on_discover(domain)

    with col2:
        if _action_btn(
            "Run All Pending", "dom_run_all",
            enabled=pending_count > 0,
            help_text="No pending subdomains to run",
            icon=":material/play_arrow:",
        ):
            if on_run_all:
                on_run_all(domain, [s for s in subdomains if s.get("status") == "pending"])

    with col3:
        if _action_btn(
            "Export Completed", "dom_export_all",
            enabled=done_count > 0,
            help_text="No completed subdomains to export",
            icon=":material/bar_chart:",
        ):
            if on_export_all:
                on_export_all(domain)

    st.markdown(
        "<hr style='border-color:#1e2533; margin:36px 0 28px;'>",
        unsafe_allow_html=True,
    )


def render_subdomain_detail(
    domain: str,
    subdomain: dict,
    pipeline_info: dict | None = None,
    tools: list[dict] | None = None,
    features: list[dict] | None = None,
    on_run: Callable | None = None,
    on_stop: Callable | None = None,
    on_export: Callable | None = None,
    on_clear: Callable | None = None,
    on_view_logs: Callable | None = None,
) -> None:
    db_status = subdomain.get("status", "pending")
    live_status = pipeline_info.get("status") if pipeline_info else None
    status = live_status if live_status in ("running", "done", "failed") else db_status

    subdomain_name = subdomain.get("name", "Unknown")
    subdomain_id = subdomain.get("id", 0)

    safe_name = sanitize_for_html(subdomain_name)
    safe_domain = sanitize_for_html(domain)
    status_svg = status_icon(status, 20)

    st.markdown(
        f"<h3 style='text-align:left; margin:0 0 4px; font-size:24px; font-weight:700;'>"
        f"{status_svg} {safe_name}</h3>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<p style='font-size:14px; color:#94a3b8; text-align:left; margin:0 0 8px;'>"
        f"Domain: <strong>{safe_domain}</strong> / Status: "
        f"<span style='color:{'#22c55e' if status=='done' else '#3b82f6' if status=='running' else '#ef4444' if status=='failed' else '#6b7280'}; font-weight:600;'>"
        f"{status.upper()}</span></p>",
        unsafe_allow_html=True,
    )
    st.markdown(
        "<hr style='border-color:#1e2533; margin:16px 0 28px;'>",
        unsafe_allow_html=True,
    )

    if status == "running" and pipeline_info:
        _section_header("Pipeline Progress")
        progress = pipeline_info.get("progress", 0.0)
        step = pipeline_info.get("step", "m2")
        message = pipeline_info.get("message", "")
        st.progress(progress, text=f"[{step.upper()}] {message}")

        stages = [
            ("M2", "Tool Discovery", progress > 0.2),
            ("M3", "Feature Discovery", progress > 0.4),
            ("M4", "Subfeature Disc.", progress > 0.6),
            ("M5", "Matrix Population", progress >= 1.0),
        ]
        stage_cols = st.columns(4)
        for i, (stage, label, done) in enumerate(stages):
            with stage_cols[i]:
                svg = icon_html("check", "ui", 14) if done else status_icon("running" if progress > 0 else "pending", 14)
                st.markdown(
                    f"<div style='text-align:center; font-size:11px; color:#94a3b8;'>"
                    f"{svg}<br/><strong>{stage}</strong><br/>{label}</div>",
                    unsafe_allow_html=True,
                )

        st.markdown(
            "<hr style='border-color:#1e2533; margin:28px 0;'>",
            unsafe_allow_html=True,
        )

    if tools or features:
        _section_header("Data Summary")
        c1, c2, c3 = st.columns(3)
        enterprise_count = sum(1 for t in (tools or []) if t.get("tool_type") == "enterprise")
        oss_count = sum(1 for t in (tools or []) if t.get("tool_type") == "opensource")
        with c1:
            st.metric("Tools", enterprise_count + oss_count)
            st.caption(f"{enterprise_count} enterprise / {oss_count} OSS")
        with c2:
            st.metric("Features", len(features or []))
        with c3:
            st.metric("Subfeatures", sum(f.get("subfeature_count", 0) for f in (features or [])))

        st.markdown(
            "<hr style='border-color:#1e2533; margin:28px 0;'>",
            unsafe_allow_html=True,
        )

    _section_header("Actions")

    row1_cols = st.columns(3)

    with row1_cols[0]:
        run_enabled = status == "pending"
        stop_enabled = status == "running"
        if stop_enabled:
            if _action_btn("Stop Pipeline", "sd_stop", True, icon=":material/stop:"):
                if on_stop:
                    on_stop(subdomain_id)
        else:
            if _action_btn(
                "Run Pipeline", "sd_run", run_enabled,
                help_text=f"Cannot run - subdomain is {status}",
                icon=":material/play_arrow:",
            ):
                if on_run:
                    on_run(domain, subdomain_id, subdomain_name)

    with row1_cols[1]:
        export_enabled = status == "done"
        if _action_btn(
            "Export", "sd_export", export_enabled,
            help_text=f"Export available once pipeline completes (currently {status})",
            icon=":material/bar_chart:",
        ):
            if on_export:
                on_export(subdomain_id, subdomain_name)

    with row1_cols[2]:
        if _action_btn("View Logs", "sd_logs", True, icon=":material/description:"):
            if on_view_logs:
                on_view_logs(subdomain_name)

    row2_cols = st.columns(2)

    with row2_cols[0]:
        clear_enabled = status in ("done", "failed")
        if _action_btn(
            "Clear Data", "sd_clear", clear_enabled,
            help_text=f"Clear available for done or failed subdomains (currently {status})",
            icon=":material/delete:",
        ):
            if on_clear:
                on_clear(subdomain_id)

    with row2_cols[1]:
        if st.button("Copy Name", key="sd_copy", use_container_width=True, icon=":material/content_copy:"):
            st.session_state._clipboard = subdomain_name
            st.success(f"Copied: {subdomain_name}")

    st.markdown(
        "<hr style='border-color:#1e2533; margin:36px 0 28px;'>",
        unsafe_allow_html=True,
    )


def render_detail_panel(
    tree_data: dict[str, list[dict]],
    pipeline_info: dict | None = None,
    tools_data: dict[int, list[dict]] | None = None,
    features_data: dict[int, list[dict]] | None = None,
    on_discover: callable = None,
    on_run: callable = None,
    on_stop: callable = None,
    on_export: callable = None,
    on_clear: callable = None,
    on_view_logs: callable = None,
    on_run_all: callable = None,
    on_export_all: callable = None,
) -> None:
    detail_item = st.session_state.get("detail_item")

    if not detail_item:
        render_empty_state()
        return

    if detail_item.startswith("domain_"):
        domain_name = detail_item.replace("domain_", "", 1)
        subdomains = tree_data.get(domain_name, [])
        render_domain_detail(
            domain_name, subdomains,
            on_discover=on_discover,
            on_run_all=on_run_all,
            on_export_all=on_export_all,
        )
        return

    if detail_item.startswith("subdomain_"):
        item_key = detail_item.replace("subdomain_", "", 1)
        parts = item_key.rsplit("_", 1)
        try:
            if len(parts) == 2:
                domain_name = parts[0]
                subdomain_id = int(parts[1])
            else:
                key_parts = item_key.split("_")
                subdomain_id = int(key_parts[-1])
                domain_name = "_".join(key_parts[:-1])
        except (ValueError, IndexError):
            st.error(f"Invalid subdomain key: {item_key}")
            render_empty_state()
            return

        subdomains = tree_data.get(domain_name, [])
        subdomain = next((s for s in subdomains if s.get("id") == subdomain_id), None)

        if not subdomain:
            st.warning(f"Subdomain not found: {item_key}")
            render_empty_state()
            return

        pipeline = pipeline_info.get(item_key) if pipeline_info else None
        tools = tools_data.get(subdomain_id) if tools_data else None
        features = features_data.get(subdomain_id) if features_data else None

        render_subdomain_detail(
            domain_name, subdomain,
            pipeline_info=pipeline,
            tools=tools,
            features=features,
            on_run=on_run,
            on_stop=on_stop,
            on_export=on_export,
            on_clear=on_clear,
            on_view_logs=on_view_logs,
        )
        return

    render_empty_state()
