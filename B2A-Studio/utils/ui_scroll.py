"""页面锚点与录制后自动滚回目标区域。"""

from __future__ import annotations

import streamlit as st
import streamlit.components.v1 as components

ANCHOR_CASTING = "b2a-casting-studio"
ANCHOR_PRONUNCIATION = "b2a-pronunciation-studio"
ANCHOR_RECORDING = "b2a-recording-studio"


def render_scroll_anchor(anchor_id: str) -> None:
    extra = " b2a-section-recording" if anchor_id == ANCHOR_RECORDING else ""
    st.markdown(
        f'<div id="{anchor_id}" class="b2a-scroll-anchor{extra}"></div>',
        unsafe_allow_html=True,
    )


def request_scroll_to(anchor_id: str) -> None:
    st.session_state["ui_scroll_anchor"] = anchor_id
    if anchor_id == ANCHOR_RECORDING:
        st.session_state.casting_collapsed = True


def apply_pending_scroll() -> None:
    anchor_id = st.session_state.pop("ui_scroll_anchor", None)
    if not anchor_id:
        return
    components.html(
        f"""
        <script>
        (function () {{
            const doc = window.parent.document;
            const el = doc.getElementById("{anchor_id}");
            if (el) {{
                el.scrollIntoView({{ behavior: "instant", block: "start" }});
            }}
        }})();
        </script>
        """,
        height=0,
    )
