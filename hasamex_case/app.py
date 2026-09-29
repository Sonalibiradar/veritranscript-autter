import os
import streamlit as st
from dotenv import load_dotenv

import importlib
import core.llm
importlib.reload(core.llm)

from core.parser import parse_all, load_interview_guide
from core.retrieval import SegmentIndex
from core.verify import verify_all_quotes
from core.llm import (
    answer_question_for_transcript,
    synthesize_themes,
    answer_freeform_question,
    set_api_key,
    set_demo_mode,
    is_demo_mode,
    MODEL,
)
from core.observability import init_observability

load_dotenv()
init_observability()

st.set_page_config(page_title="Expert Call Analyser", layout="wide")
st.title("🎙️ Expert Call Transcript Analyser")
st.caption("Hasamex AI Engineer case study — answers, quotes, and themes grounded in the source transcripts only.")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


# ---------------------------------------------------------------------------
# Load & cache data
# ---------------------------------------------------------------------------

@st.cache_resource
def load_data():
    segments = parse_all(DATA_DIR)
    guide_questions = load_interview_guide(os.path.join(DATA_DIR, "Interview_Guide.txt"))
    index = SegmentIndex(segments)
    return segments, guide_questions, index


segments, guide_questions, index = load_data()
transcripts = sorted(set(s.transcript_id for s in segments))

# ---------------------------------------------------------------------------
# Sidebar: Transcripts & API / Demo Mode Configuration
# ---------------------------------------------------------------------------

with st.sidebar:
    st.subheader("Loaded Transcripts")
    for tid in transcripts:
        expert = next(s.expert_name for s in segments if s.transcript_id == tid)
        market = next(s.market for s in segments if s.transcript_id == tid)
        st.write(f"**{expert}** — {market}")
    st.divider()

    st.subheader("⚙️ Settings & Mode")
    raw_env_key = os.environ.get("ANTHROPIC_API_KEY", "")
    has_placeholder = (
        not raw_env_key
        or "your_anthropic_api_key_here" in raw_env_key
        or raw_env_key == "sk-ant-api03-..."
    )

    api_key_input = st.text_input(
        "Anthropic API Key",
        type="password",
        value="" if has_placeholder else raw_env_key,
        help="Paste your Anthropic API key here (sk-ant-...) or leave blank to use Demo Mode.",
    )

    if api_key_input and api_key_input.strip() != raw_env_key:
        set_api_key(api_key_input.strip())
        st.success("API key updated!")

    # Toggle for Demo/Offline Mode
    demo_mode_toggle = st.checkbox(
        "⚡ Demo / Offline Mode",
        value=has_placeholder and not api_key_input,
        help="Use pre-computed grounded answers and exact quote verifications without calling external APIs.",
    )
    set_demo_mode(demo_mode_toggle)

    if demo_mode_toggle:
        st.info("💡 **Demo Mode Active**: Instant grounded answers and verified quotes, no API key needed.")
    elif api_key_input or (raw_env_key and not has_placeholder):
        st.success(f"🟢 **Claude Connected** (`{MODEL}`)")
    else:
        st.warning("⚠️ Enter an Anthropic API Key or enable **Demo Mode** above.")

    if os.environ.get("AUTTER_RUNTIME_KEY"):
        st.caption("🛡️ **Autter Runtime**: Observability & LLM Tracing Active")


def quote_badge(v):
    return "✅ verified" if v["verified"] else "⚠️ not found in transcript"


def render_answer_block(expert_label, result, segments_for_verification, transcript_id=None):
    if not result.get("covered"):
        st.write(f"*{expert_label}: not addressed in this transcript.*")
        return
    st.write(f"**{expert_label}:** {result['answer']}")
    quotes = result.get("quotes", [])
    if quotes:
        checks = verify_all_quotes([q["text"] for q in quotes], segments_for_verification, transcript_id)
        for q, chk in zip(quotes, checks):
            ts = chk["matched_segment"]["timestamp"] if chk.get("matched_segment") else q.get("timestamp", "?")
            st.markdown(f"> \"{q['text']}\" — `[{ts}]` &nbsp; {quote_badge(chk)}")


tab1, tab2, tab3 = st.tabs(["📋 Interview Guide", "🔍 Themes & Disagreements", "💬 Ask a Question"])

# ---------------------------------------------------------------------------
# TAB 1 — Interview guide answers per expert
# ---------------------------------------------------------------------------
with tab1:
    st.subheader("Interview guide answers, per expert")
    if st.button("Run analysis for all guide questions", type="primary"):
        results_by_question = {}
        progress = st.progress(0.0)
        try:
            for i, q in enumerate(guide_questions):
                retrieved = index.search_all_transcripts(q, top_k_per_transcript=3)
                per_expert = []
                for tid, segs in retrieved.items():
                    res = answer_question_for_transcript(q, segs)
                    expert_name = segs[0].expert_name if segs else tid
                    market = segs[0].market if segs else ""
                    per_expert.append({"transcript_id": tid, "expert_name": expert_name, "market": market, **res})
                results_by_question[q] = per_expert
                progress.progress((i + 1) / len(guide_questions))
            st.session_state["results_by_question"] = results_by_question
            st.session_state["retrieved_by_question"] = {
                q: index.search_all_transcripts(q, top_k_per_transcript=3) for q in guide_questions
            }
        except Exception as e:
            st.error(f"Error executing analysis: {e}")

    if "results_by_question" in st.session_state:
        for q, per_expert in st.session_state["results_by_question"].items():
            with st.expander(q, expanded=False):
                for a in per_expert:
                    segs_for_this = [s for s in segments if s.transcript_id == a["transcript_id"]]
                    render_answer_block(f"{a['expert_name']} ({a['market']})", a, segs_for_this, a["transcript_id"])
    else:
        st.info("Click the button above to generate grounded answers for every guide question, per expert.")

# ---------------------------------------------------------------------------
# TAB 2 — Themes & disagreements
# ---------------------------------------------------------------------------
with tab2:
    st.subheader("Common themes & disagreements across experts")
    if "results_by_question" not in st.session_state:
        st.info("Run the Interview Guide analysis in the first tab first.")
    else:
        if st.button("Synthesise themes & disagreements", type="primary"):
            try:
                themes_by_question = {}
                for q, per_expert in st.session_state["results_by_question"].items():
                    themes_by_question[q] = synthesize_themes(q, per_expert)
                st.session_state["themes_by_question"] = themes_by_question
            except Exception as e:
                st.error(f"Error synthesising themes: {e}")

        if "themes_by_question" in st.session_state:
            for q, t in st.session_state["themes_by_question"].items():
                with st.expander(q, expanded=False):
                    st.markdown("**Common themes**")
                    if t.get("common_themes"):
                        for theme in t["common_themes"]:
                            experts = ", ".join(theme.get("supporting_experts", []))
                            st.write(f"- {theme['theme']} _(supported by: {experts})_")
                    else:
                        st.write("_None identified._")

                    st.markdown("**Disagreements**")
                    if t.get("disagreements"):
                        for d in t["disagreements"]:
                            st.write(f"- **{d['topic']}**")
                            for p in d.get("positions", []):
                                st.write(f"   - {p['expert']}: {p['position']}")
                    else:
                        st.write("_None identified — experts broadly aligned._")

# ---------------------------------------------------------------------------
# TAB 3 — Free-form Q&A across all transcripts
# ---------------------------------------------------------------------------
with tab3:
    st.subheader("Ask a question across all three transcripts")
    st.caption("Try: *Do any experts disagree on how important ROI is?* or *What is the typical hospital purchase timeline?*")

    user_q = st.text_input("Your question", placeholder="e.g. Do any experts disagree on how important ROI is?")
    if st.button("Ask") and user_q.strip():
        retrieved = index.search_all_transcripts(user_q, top_k_per_transcript=4)
        with st.spinner("Retrieving relevant excerpts and generating a grounded answer..."):
            try:
                result = answer_freeform_question(user_q, retrieved)
                if not result.get("covered"):
                    st.warning("The transcripts don't contain enough information to answer this confidently.")
                else:
                    st.write(result["answer"])
                    quotes = result.get("quotes", [])
                    if quotes:
                        st.markdown("**Supporting quotes:**")
                        for q in quotes:
                            matching_segs = [s for s in segments if s.expert_name == q.get("expert", "")]
                            chk = verify_all_quotes([q["text"]], matching_segs or segments)[0]
                            ts = chk["matched_segment"]["timestamp"] if chk.get("matched_segment") else q.get("timestamp", "?")
                            st.markdown(f"> \"{q['text']}\" — **{q.get('expert','?')}** `[{ts}]` &nbsp; {quote_badge(chk)}")
            except Exception as e:
                st.error(f"Error generating answer: {e}")
