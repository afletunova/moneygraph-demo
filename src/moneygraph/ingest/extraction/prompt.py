"""
Extraction prompts — SIMPLIFIED for the public example repo.

The production version of this file has substantially more tuned business
logic: dozens of named-example edge cases (specific companies, specific
exclusion patterns for IPOs/underwriters/service contracts, syndicate-round
attribution rules, etc.), refined over many real extraction failures. This
version keeps the same function signatures, the same JSON schema, and the
same general shape (a system prompt + a couple of guardrail rules) so the
surrounding pipeline code (gates, backends, parsing) is a faithful example of
the real architecture — it's just not fed the real tuned wording.
"""

import hashlib
import json
import logging
import re

logger = logging.getLogger(__name__)

_MAX_CONTENT_CHARS = 10_000

_SYSTEM_PROMPT = """\
You extract investment relationships from SEC filings and financial news.

Return ONLY valid JSON, no other text. Schema:
{
  "events": [
    {
      "investor": "<full legal name of the investing entity>",
      "investee": "<full legal name of the company receiving investment>",
      "amount_usd": <integer dollars, or null if not stated>,
      "date": "<YYYY-MM-DD>",
      "delta_usd": <signed integer: positive for new investment, negative for divestment/exit>,
      "event_type": "<investment|partial_exit|full_exit|cancelled|correction>",
      "edge_type": "<ownership|subsidiary|joint_venture>",
      "confidence": "<high|medium|low>",
      "excerpt": "<relevant quote from the text, max 500 chars>"
    }
  ]
}

Rules:
- Only include events where equity or equivalent value changes hands (not loans, grants, or service contracts).
- "investor" and "investee" must each be a specific named organization, never a collective noun
  (e.g. "several investors" is invalid; omit the event).
- If the amount is a range, use the midpoint.
- confidence=high: specific dollar amount from the filing; medium: amount confirmed but imprecise; low: inferred.
- If no investment events are found, return {"events": []}"""

# Short hash used to detect stale batch results at harvest time.
PROMPT_VERSION: str = hashlib.sha256(_SYSTEM_PROMPT.encode()).hexdigest()[:16]


def build_user_content(text: str, form_type: str, node_name: str) -> str:
    return f"Filing type: {form_type}\nFiling node (company that filed): {node_name}\n\n{text[:_MAX_CONTENT_CHARS]}"


# ---------------------------------------------------------------------------
# Web search prompt variant
# ---------------------------------------------------------------------------

WEB_SYSTEM_PROMPT = """\
You extract investment relationships from news articles and web sources.

Return ONLY valid JSON, no other text. Schema:
{
  "events": [
    {
      "investor": "<full legal name of the investing entity>",
      "investee": "<full legal name of the company receiving investment>",
      "amount_usd": <integer dollars, or null if not stated>,
      "date": "<YYYY-MM-DD, or null if not stated>",
      "delta_usd": <signed integer: positive for new investment, negative for divestment/exit>,
      "event_type": "<investment|partial_exit|full_exit|cancelled|correction>",
      "edge_type": "<ownership|subsidiary|joint_venture>",
      "confidence": "<high|medium|low>",
      "excerpt": "<verbatim quote copied from the article text, max 500 chars>"
    }
  ]
}

Rules:
- ONLY extract events explicitly described as a closed deal or an executed agreement.
  Ignore anything described as rumored, "in talks", or "considering" — when in doubt, omit.
- Only include events where equity or equivalent value changes hands (not loans or service contracts).
- "investor" and "investee" must each be a specific named organization, never a collective noun.
- The excerpt field MUST be copied verbatim from the provided article text — do not paraphrase.
- If no qualifying investment events are found, return {"events": []}"""


def build_web_user_content(text: str, url: str) -> str:
    return f"Source URL: {url}\n\n{text[:_MAX_CONTENT_CHARS]}"


def parse_extraction_response(raw: str) -> list[dict]:
    """
    Parse a model response into a list of event dicts.

    Tries direct JSON parse first (expected path with response_format=json_object).
    Falls back to greedy outer-object brace extraction as defense-in-depth.
    Returns [] on total failure.
    """
    raw = raw.strip()
    try:
        return json.loads(raw).get("events", [])
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        logger.warning("model returned non-JSON; raw=%s", raw[:200])
        return []
    try:
        return json.loads(match.group(0)).get("events", [])
    except json.JSONDecodeError:
        logger.warning("model returned malformed JSON; raw=%s", raw[:200])
        return []
