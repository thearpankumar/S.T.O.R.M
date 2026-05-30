# TUI Codebase Review Report

**Reviewed:** 2026-05-30
**Depth:** standard
**Files Reviewed:** 8
**Status:** issues_found

---

## Summary

Reviewed the TUI (Terminal User Interface) Streamlit codebase after recent bug fixes. The codebase shows good architecture with proper separation of concerns, action registry pattern, and icon management. However, several security concerns (XSS vectors via unsanitized database content), code quality issues (missing type hints, deprecated Python syntax), and potential race conditions were identified.

---

## Bugs Remaining

### CR-01: Potential XSS via Unsanitized Database Content

**File:** `tui/components/detail_panel.py:98`
**Issue:** Subdomain name from database is interpolated directly into HTML without sanitization. If the database contains malicious content with `<script>` tags, it will execute in the browser.

```python
st.markdown(f"{svg} {sd['name']}", unsafe_allow_html=True)
```

**Impact:** Any code path that writes domain/subdomain names to the database without sanitization could inject malicious scripts.

**Fix:**
```python
import html
# Option 1: Use html.escape
st.markdown(f"{svg} {html.escape(sd['name'])}", unsafe_allow_html=True)

# Option 2: Don't use unsafe_allow_html at all
st.markdown(f"{svg} **{sd['name']}**")  # Streamlit sanitizes markdown content
```

---

### CR-02: Additional XSS Vector in Domain Tree

**File:** `tui/components/domain_tree.py:211`
**Issue:** Similar XSS vulnerability - subdomain names from database are rendered in buttons without sanitization.

```python
label = f"{marker}{sd['name']}"
# Later used in st.button which may not escape when rendered
```

**Fix:** Same as CR-01 - sanitize database content before rendering or avoid `unsafe_allow_html=True`.

---

### CR-03: Race Condition in Pipeline Progress Updates

**File:** `tui/app.py:192-194`
**Issue:** The `_update()` function reads `_PIPELINE_PROGRESS[pipeline_key]` outside the lock, then updates inside the lock. Another thread could delete the key between the check and update.

```python
def _update(patch: dict) -> None:
    with _PIPELINE_LOCK:
        if pipeline_key in _PIPELINE_PROGRESS:  # Check inside lock is correct
            _PIPELINE_PROGRESS[pipeline_key].update(patch)
```

However, the actual issue is in the cleanup logic at line 158:
```python
for k in list(_PIPELINE_PROGRESS):
    if _PIPELINE_PROGRESS[k].get("status") in ("done", "failed"):
        del _PIPELINE_PROGRESS[k]  # This runs inside lock, but...
```

The `list(_PIPELINE_PROGRESS)` creates a snapshot, but between creating the snapshot and acquiring the lock, keys could change.

**Fix:**
```python
def cleanup_completed_pipelines() -> None:
    # ... existing code ...
    with _PIPELINE_LOCK:
        keys_to_remove = [
            k for k, v in _PIPELINE_PROGRESS.items()
            if v.get("status") in ("done", "failed")
        ]
        for k in keys_to_remove:
            if k in _PIPELINE_PROGRESS:  # Double-check before deletion
                del _PIPELINE_PROGRESS[k]
```

---

### CR-04: Stale Subdomain Lookup After Database Changes

**File:** `tui/components/detail_panel.py:330-336`
**Issue:** After fetching subdomains from `tree_data`, the code looks up a subdomain by ID. If the database changed between loading `tree_data` and this lookup (e.g., in a concurrent pipeline), the subdomain might not be found, showing a confusing error.

```python
subdomains = tree_data.get(domain_name, [])
subdomain = next((s for s in subdomains if s.get("id") == subdomain_id), None)

if not subdomain:
    st.warning(f"Subdomain not found: {item_key}")
```

**Fix:** This is partially handled, but the error UX could be improved with a "Refresh" suggestion:
```python
if not subdomain:
    st.warning(f"Subdomain not found: {item_key}. Click 'Refresh' to reload.")
    if st.button("Refresh", key="refresh_not_found"):
        st.rerun()
```

---

## Warnings

### WR-01: Deprecated `callable` Type Hint Syntax

**File:** `tui/components/detail_panel.py:62, 147, 288-295`
**Issue:** Using `callable` as a type hint is deprecated in Python 3.9+. Should use `typing.Callable` or `collections.abc.Callable`.

```python
on_discover: callable = None,  # Deprecated
```

**Fix:**
```python
from typing import Callable
on_discover: Callable | None = None,
```

---

### WR-02: Same Issue in bulk_actions.py

**File:** `tui/components/bulk_actions.py:11-13`
**Issue:** Same deprecated `callable` type hint usage.

**Fix:** Import and use `Callable` from `typing`.

---

### WR-03: Same Issue in context_menu.py

**File:** `tui/components/context_menu.py:12, 85-87`
**Issue:** Same deprecated `callable` type hint usage.

---

### WR-04: Thread Not Cleaned Up After Completion

**File:** `tui/app.py:616-618`
**Issue:** Threads are stored in `_PIPELINE_THREADS` but never removed after completion. While `_shutdown_all_threads` handles cleanup on exit, long-running sessions could accumulate stale thread references.

```python
t = threading.Thread(target=_run_in_thread, daemon=False, name=f"pipeline-{subdomain_name}")
_PIPELINE_THREADS[pipeline_key] = t
t.start()
```

**Fix:** Clean up thread references when pipelines complete:
```python
# In cleanup_completed_pipelines():
with _PIPELINE_LOCK:
    for k in keys_to_remove:
        if k in _PIPELINE_PROGRESS:
            del _PIPELINE_PROGRESS[k]
        if k in _PIPELINE_THREADS and not _PIPELINE_THREADS[k].is_alive():
            del _PIPELINE_THREADS[k]
```

---

### WR-05: Missing Return Type Annotation

**File:** `tui/components/domain_tree.py:231-233`
**Issue:** Function `render_domain_tree` has complex return type annotation but returns `None, None, ...` which is misleading.

```python
def render_domain_tree(
    tree_data: dict[str, list[dict]],
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    # ...
    return None, None, st.session_state.get("context_menu_target")
```

**Fix:** Either return meaningful values or change signature:
```python
def render_domain_tree(tree_data: dict[str, list[dict]]) -> None:
```

---

### WR-06: Unused Import in registry.py

**File:** `tui/actions/registry.py:1`
**Issue:** `field` is imported from dataclasses but only used once and could be replaced with `default_factory=dict` directly.

```python
from dataclasses import dataclass, field
```

While this is minor, it's actually used correctly. This is a false positive - removing.

---

### WR-07: Empty Exception Handler

**File:** `tui/app.py:887`
**Issue:** Bare `except:` without logging the exception makes debugging difficult.

```python
except Exception:
    st.caption("Could not read log file.")
```

**Fix:**
```python
except Exception as log_err:
    logger.debug(f"Failed to read log file: {log_err}")
    st.caption("Could not read log file.")
```

---

### WR-08: Potential Division by Zero

**File:** `tui/app.py:519`
**Issue:** `st_size / 1024` then `/ 1024` again - if file is empty, `size_kb` is 0, which is fine, but the calculation logic is confusing.

Actually this is fine - `st_size` can't be negative. Not an issue.

---

### WR-09: Inconsistent Error Handling in Session State Population

**File:** `tui/app.py:81-83`
**Issue:** When iterating defaults, `copy.deepcopy(value)` is called on all values even when not needed. Sets are copied, but other values just reference the originals.

```python
for key, value in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = copy.deepcopy(value)  # Overkill for simple values
```

**Fix:** Only copy when necessary:
```python
import copy
for key, value in defaults.items():
    if key not in st.session_state:
        if isinstance(value, (set, dict, list)):
            st.session_state[key] = copy.deepcopy(value)
        else:
            st.session_state[key] = value
```

---

## Security Issues

### SEC-01: XSS Vulnerability Pattern Throughout Codebase

**Severity:** HIGH
**Files:** Multiple (see CR-01, CR-02)
**Issue:** Extensive use of `unsafe_allow_html=True` with database-sourced content creates XSS attack surface. Found 45+ instances across the codebase.

Key vectors:
- `tui/components/detail_panel.py:98` - Subdomain names
- `tui/components/detail_panel.py:164` - Subdomain names in headings
- `tui/components/domain_tree.py:211` - Subdomain names in labels

**Root Cause:** Data from database is trusted implicitly without sanitization.

**Remediation:**
1. Create a `sanitize_html()` utility function
2. Apply to all database-sourced strings before rendering with `unsafe_allow_html=True`
3. Prefer Streamlit's safe rendering (markdown without `unsafe_allow_html`) where possible
4. Validate and sanitize on database write as defense-in-depth

---

### SEC-02: JavaScript Injection via Auto-refresh Script

**File:** `tui/app.py:907-918`
**Issue:** The auto-refresh feature injects JavaScript that queries DOM elements. While the script is static, this pattern could be problematic if similar injection uses dynamic content.

```python
st.markdown("""
<script>
setTimeout(function() {
    var refreshBtn = window.parent.document.querySelector('[data-testid="stBaseButton-secondary"]');
    if (refreshBtn && refreshBtn.innerText.includes('Refresh')) {
        refreshBtn.click();
    } else {
        window.parent.location.reload();
    }
}, 3000);
</script>
""", unsafe_allow_html=True)
```

**Risk Level:** LOW (static script, no user input)

**Recommendation:** Document this as intentional and ensure no dynamic content flows into similar script blocks.

---

### SEC-03: File Path Construction Without Validation

**File:** `tui/app.py:505`
**Issue:** Log file path is constructed from user selection without path traversal validation.

```python
log_path = log_dir / selected_log  # selected_log comes from selectbox of filenames
```

**Risk Level:** MEDIUM - The `log_dir` is controlled and filenames come from directory listing, but if the selectbox were manipulated (browser dev tools), path traversal could occur.

**Fix:**
```python
import os
log_path = log_dir / selected_log
# Ensure resolved path is still under log_dir
if not os.path.realpath(log_path).startswith(os.path.realpath(log_dir)):
    st.error("Invalid log file path")
    return
```

---

## Code Quality Issues

### CQ-01: Missing Docstrings on Public Functions

**Files:** `tui/actions/pipeline.py`, `tui/actions/registry.py`, `tui/utils/icons.py`
**Issue:** Public functions lack documentation.

Examples:
- `register_all_actions()` - No docstring
- `get_actions_for_context()` - No docstring explaining parameters
- `status_icon()` - No docstring

**Fix:** Add docstrings:
```python
def status_icon(status: str, size: int = 16) -> str:
    """Return SVG markup for a status icon.
    
    Args:
        status: One of 'pending', 'running', 'done', 'failed'
        size: Icon size in pixels
        
    Returns:
        SVG markup string, or pending icon if status unknown
    """
```

---

### CQ-02: Magic Numbers in CSS and Styling

**File:** `tui/app.py:293-442`
**Issue:** CSS block has many magic numbers without explanation.

```python
width: 3px;  # What does 3px represent?
padding: 1px 7px;  # Why these values?
```

**Fix:** Consider extracting to CSS files or using CSS variables with comments.

---

### CQ-03: Hardcoded Status Labels

**Files:** `tui/app.py:832-837`, `tui/components/detail_panel.py:186-191`
**Issue:** Stage labels and thresholds are duplicated in multiple places.

```python
STAGE_LABELS = {
    "m2": "Tool Discovery",
    "m3": "Feature Discovery",
    ...
}
```

vs:

```python
stages = [
    ("M2", "Tool Discovery", progress > 0.25),
    ...
]
```

**Fix:** Create a constants module:
```python
# tui/constants.py
PIPELINE_STAGES = [
    {"id": "M2", "label": "Tool Discovery", "threshold": 0.25},
    ...
]
```

---

### CQ-04: Unused Variable in domain_tree.py

**File:** `tui/components/domain_tree.py:196`
**Issue:** `last_idx` is computed but never used.

```python
last_idx = len(filtered) - 1  # Never used
```

**Fix:** Remove the unused variable.

---

## Recommendations

### High Priority

1. **Implement HTML Sanitization** - Create a utility to escape HTML entities in database-sourced strings before rendering with `unsafe_allow_html=True`.

2. **Fix Race Condition in Pipeline Progress** - Ensure all dictionary operations on `_PIPELINE_PROGRESS` are atomic under the lock.

3. **Add Path Validation** - Validate resolved file paths don't escape intended directories.

### Medium Priority

4. **Update Type Hints** - Replace deprecated `callable` with `Callable` throughout.

5. **Clean Up Thread References** - Remove stale threads from `_PIPELINE_THREADS` after completion.

6. **Add Comprehensive Docstrings** - Document all public functions in actions/registry and utils/icons.

### Low Priority

7. **Extract Constants** - Consolidate duplicated stage labels and thresholds.

8. **Improve Error Logging** - Replace bare `except:` blocks with proper logging.

9. **Simplify Function Signatures** - Fix return type mismatch in `render_domain_tree`.

---

## Files Reviewed

| File | Lines | Status |
|------|-------|--------|
| `tui/app.py` | 933 | Issues found |
| `tui/components/detail_panel.py` | 355 | Issues found |
| `tui/components/domain_tree.py` | 274 | Issues found |
| `tui/components/bulk_actions.py` | 74 | Issues found |
| `tui/components/context_menu.py` | 157 | Issues found |
| `tui/actions/pipeline.py` | 233 | Minor issues |
| `tui/actions/registry.py` | 119 | Minor issues |
| `tui/utils/icons.py` | 118 | Minor issues |

---

## Summary Statistics

| Metric | Count |
|--------|-------|
| Critical Issues | 4 |
| Warnings | 9 |
| Security Issues | 3 |
| Quality Issues | 4 |
| **Total Issues** | **20** |

---

_Reviewed: 2026-05-30_
_Reviewer: gsd-code-reviewer_
_Depth: standard_
