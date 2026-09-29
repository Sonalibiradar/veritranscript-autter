"""
All LLM calls go through this module. Every prompt enforces:
  - answer ONLY from the provided transcript segments (no outside knowledge)
  - explicit "not covered" if the transcript doesn't address the question
  - every claim backed by an exact quote + timestamp
  - strict JSON output so the app can parse + verify it programmatically

Swap providers by changing `call_llm()` — the rest of the app doesn't care
whether it's Claude, GPT, or a local model, as long as it returns text.
"""

import os
import json
import re
from typing import List, Dict

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from .demo_data import DEMO_GUIDE_ANSWERS, DEMO_THEMES, get_demo_freeform_answer
from .observability import capture_warning

tracer = trace.get_tracer("hasamex-case-llm")
MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-4-5")
_client = None
_demo_mode = False


def is_demo_mode() -> bool:
    global _demo_mode
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    is_placeholder = not api_key or "your_anthropic_api_key_here" in api_key or api_key == "sk-ant-api03-..."
    return _demo_mode or is_placeholder


def set_demo_mode(enabled: bool):
    global _demo_mode
    _demo_mode = enabled


def set_api_key(api_key: str, model: str = None):
    global _client, MODEL, _demo_mode
    if model:
        MODEL = model
    os.environ["ANTHROPIC_API_KEY"] = api_key.strip()
    _client = None  # force recreation
    _demo_mode = False


def _get_client():
    """Lazy import + lazy init: keeps this module importable (and unit-testable
    with a mocked call_llm) even if the `anthropic` package isn't installed yet."""
    global _client
    if _client is None:
        import anthropic  # imported here, not at module level, on purpose
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key or "your_anthropic_api_key_here" in api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set or still contains a placeholder. "
                "Please enter your Anthropic API key in .env or the sidebar, or enable Demo Mode."
            )
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def call_llm(system: str, user: str, max_tokens: int = 1000) -> str:
    client = _get_client()
    with tracer.start_as_current_span(
        f"chat {MODEL}",
        kind=SpanKind.CLIENT,
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.system": "anthropic",
            "gen_ai.request.model": MODEL,
            "gen_ai.request.max_tokens": max_tokens,
        },
    ) as span:
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            if hasattr(resp, "usage") and resp.usage:
                if hasattr(resp.usage, "input_tokens"):
                    span.set_attribute("gen_ai.usage.input_tokens", resp.usage.input_tokens)
                if hasattr(resp.usage, "output_tokens"):
                    span.set_attribute("gen_ai.usage.output_tokens", resp.usage.output_tokens)
            if hasattr(resp, "model"):
                span.set_attribute("gen_ai.response.model", resp.model)

            return "".join(block.text for block in resp.content if block.type == "text")
        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            error_msg = str(e)
            if "authentication_error" in error_msg or "invalid x-api-key" in error_msg:
                raise RuntimeError(
                    "Invalid Anthropic API Key (401 Authentication Error). "
                    "Please check your API key in .env / the sidebar, or toggle 'Demo Mode' in the sidebar to run without an active key."
                ) from e
            raise e


def _extract_json(raw: str) -> Dict:
    """Models occasionally wrap JSON in markdown fences or add stray text; strip that defensively."""
    cleaned = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)
    return json.loads(cleaned)


def _match_question_num(question: str) -> int:
    q = question.lower()
    if "current adoption" in q:
        return 1
    if "barriers" in q:
        return 2
    if "budgets" in q or "roi" in q:
        return 3
    if "training" in q or "outcomes" in q:
        return 4
    if "trend" in q or "3" in q and "5" in q:
        return 5
    if "timeline" in q or "decision-making" in q:
        return 6
    return 0


# ---------------------------------------------------------------------------
# 1. Answer a single interview-guide question, for ONE transcript
# ---------------------------------------------------------------------------

ANSWER_SYSTEM = """You are analysing an expert-interview transcript for a market research project.
You must answer STRICTLY using the transcript excerpts provided below — never use outside knowledge,
never guess, and never invent a quote or timestamp.

Return ONLY valid JSON, no markdown fences, no commentary, in this exact shape:
{
  "covered": true or false,
  "answer": "a concise 1-3 sentence answer in your own words, or empty string if not covered",
  "quotes": [
    {"text": "exact verbatim quote copied from the excerpts", "timestamp": "MM:SS"}
  ]
}
Rules:
- "quotes" must be copied EXACTLY (verbatim, no paraphrasing) from the excerpts, including the timestamp attached to that excerpt.
- Include 1-2 of the strongest supporting quotes, not every sentence.
- If the excerpts do not address the question at all, set "covered": false, "answer": "", "quotes": [].
"""


def answer_question_for_transcript(question: str, segments) -> Dict:
    tid = segments[0].transcript_id if segments else ""
    q_num = _match_question_num(question)

    if is_demo_mode():
        if (q_num, tid) in DEMO_GUIDE_ANSWERS:
            return DEMO_GUIDE_ANSWERS[(q_num, tid)]

    excerpt_block = "\n".join(f"[{s.timestamp}] {s.speaker}: {s.text}" for s in segments)
    user = f"""Interview guide question: "{question}"

Transcript excerpts:
{excerpt_block}

Respond with the JSON object only."""
    try:
        raw = call_llm(ANSWER_SYSTEM, user)
        return _extract_json(raw)
    except Exception as e:
        # Fallback to demo answer if available, else return error
        if (q_num, tid) in DEMO_GUIDE_ANSWERS:
            capture_warning("llm.guide_answer.fallback", f"LLM error: {e}. Falling back to demo answer.")
            return DEMO_GUIDE_ANSWERS[(q_num, tid)]
        return {"covered": False, "answer": "", "quotes": [], "_raw_error": str(e)}


# ---------------------------------------------------------------------------
# 2. Cross-transcript theme & disagreement synthesis for ONE guide question
# ---------------------------------------------------------------------------

THEMES_SYSTEM = """You compare how three different experts answered the same interview question.
You are given each expert's answer (already grounded in their transcript) plus their supporting quotes.
Identify what they agree on and where they genuinely differ. Do not invent anything beyond what's given.

Return ONLY valid JSON, no markdown fences, in this exact shape:
{
  "common_themes": [
    {"theme": "short description", "supporting_experts": ["Expert A", "Expert B", "Expert C"]}
  ],
  "disagreements": [
    {"topic": "short description", "positions": [{"expert": "Expert A", "position": "short summary of their stance"}]}
  ]
}
If there is no real disagreement, return an empty "disagreements" list — do not manufacture one.
"""


def synthesize_themes(question: str, per_expert_answers: List[Dict]) -> Dict:
    q_num = _match_question_num(question)
    if is_demo_mode() and q_num in DEMO_THEMES:
        return DEMO_THEMES[q_num]

    block = "\n\n".join(
        f"{a['expert_name']} ({a['market']}): {a['answer']}\n"
        + "\n".join(f'  Quote [{q["timestamp"]}]: "{q["text"]}"' for q in a.get("quotes", []))
        for a in per_expert_answers
        if a.get("covered")
    )
    if not block:
        return {"common_themes": [], "disagreements": []}

    user = f"""Interview guide question: "{question}"

Expert answers:
{block}

Respond with the JSON object only."""
    try:
        raw = call_llm(THEMES_SYSTEM, user, max_tokens=800)
        return _extract_json(raw)
    except Exception as e:
        if q_num in DEMO_THEMES:
            capture_warning("llm.themes.fallback", f"LLM error: {e}. Falling back to demo themes.")
            return DEMO_THEMES[q_num]
        return {"common_themes": [], "disagreements": [], "_raw_error": str(e)}


# ---------------------------------------------------------------------------
# 3. Free-form Q&A across all transcripts
# ---------------------------------------------------------------------------

CHAT_SYSTEM = """You answer a user's question using ONLY the transcript excerpts provided, which may
come from multiple experts across multiple markets. Never use outside knowledge. If the excerpts
don't contain enough to answer, say so explicitly rather than guessing.

Return ONLY valid JSON, no markdown fences, in this exact shape:
{
  "covered": true or false,
  "answer": "a clear answer synthesised across the relevant experts, in your own words",
  "quotes": [
    {"text": "exact verbatim quote", "timestamp": "MM:SS", "expert": "expert name"}
  ]
}
"""


def answer_freeform_question(question: str, segments_by_transcript: Dict[str, List]) -> Dict:
    if is_demo_mode():
        demo_res = get_demo_freeform_answer(question)
        if demo_res.get("covered"):
            return demo_res

    blocks = []
    for tid, segs in segments_by_transcript.items():
        if not segs:
            continue
        expert = segs[0].expert_name
        market = segs[0].market
        lines = "\n".join(f"  [{s.timestamp}] {s.speaker}: {s.text}" for s in segs)
        blocks.append(f"{expert} ({market}):\n{lines}")
    excerpt_block = "\n\n".join(blocks)

    user = f"""User question: "{question}"

Transcript excerpts:
{excerpt_block}

Respond with the JSON object only."""
    try:
        raw = call_llm(CHAT_SYSTEM, user, max_tokens=900)
        return _extract_json(raw)
    except Exception as e:
        demo_res = get_demo_freeform_answer(question)
        if demo_res.get("covered"):
            capture_warning("llm.freeform.fallback", f"LLM error: {e}. Falling back to demo freeform answer.")
            return demo_res
        return {"covered": False, "answer": "", "quotes": [], "_raw_error": str(e)}
