import httpx
import logging
import re

from config import settings

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
]


def _extract_first_name(contact_name: str, email: str) -> str:
    """
    1. contact_name first word if real
    2. email prefix stripped of numbers/symbols → first word
       athuldev743@gmail.com → Athuldev
       john.doe@company.com  → John
    3. empty string → greeting becomes "Hi,"
    """
    if contact_name and contact_name.strip() and "@" not in contact_name:
        name = contact_name.strip().split()[0].title()
        if len(name) > 1:
            return name

    if email and "@" in email:
        prefix = email.split("@")[0]
        clean  = re.sub(r"[^a-zA-Z]", " ", prefix).strip()
        parts  = [p for p in clean.split() if len(p) > 1]
        if parts:
            return parts[0].title()

    return ""


def _resolve_company(lead_company: str, email: str) -> str:
    """Return best company name or empty string."""
    bad = {"your company", "none", "unknown", "", "company", "undefined", "null", "—"}

    if lead_company and lead_company.strip().lower() not in bad:
        return lead_company.strip()

    if email and "@" in email:
        domain  = email.split("@")[1]
        generic = {
            "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
            "rediffmail.com", "yahoo.co.in", "live.com", "icloud.com", "protonmail.com",
        }
        if domain not in generic:
            return domain.split(".")[0].title()

    return ""


async def personalise_email(
    subject:                    str,
    body:                       str,
    lead_name:                  str,
    lead_company:               str,
    business_details:           str,
    sender_name:                str,
    sender_company:             str,
    sender_company_description: str = "",
    lead_email:                 str = "",
) -> dict:
    """
    Fixed email structure — always consistent regardless of lead source:

        Hi [first_name],     ← or "Hi," if no name

        [S1: what sender_company does — 1 sentence]
        [S2: how sender_company helps lead_company — uses real company name]

        Worth a quick 15-min chat to see if it fits?
        No pressure — feel free to ignore this if it's not relevant.

    Sign-off added by gmail_sender.py — not here.
    """

    first_name  = _extract_first_name(lead_name, lead_email)
    target_co   = _resolve_company(lead_company, lead_email)
    biz         = (business_details or "").strip()[:400]
    sender_desc = (sender_company_description or "").strip()

    greeting = f"Hi {first_name}," if first_name else "Hi,"

    subject_line = (
        subject
        .replace("{lead_name}",    first_name or "")
        .replace("{lead_company}", target_co  or "your business")
    ).strip() or f"Quick question for {target_co or 'you'}"

    # ── Build lead context ────────────────────────────────────────────────────
    # This is what we know about the lead — passed explicitly to AI.
    # Never use python `or` fallback strings inside the prompt itself.
    if target_co and biz:
        lead_context = f'Company: "{target_co}". What they do: "{biz}"'
    elif target_co:
        lead_context = f'Company: "{target_co}". No description — infer from the name what they likely do.'
    elif biz:
        lead_context = f'Business description: "{biz}". Company name unknown.'
    else:
        lead_context = "No lead company info available. Keep sentence 2 generic."

    # ── What to say in sentence 2 about the lead ─────────────────────────────
    # Fix: never pass a fallback string like "their company" into the prompt.
    # If we have no company name, instruct AI to write generically instead.
    if target_co:
        s2_instruction = (
            f'Must use "{target_co}" by name. '
            f'Connect to what they do (from lead context). '
            f'Name a concrete outcome: leads not missed, follow-ups automated, time saved, etc.'
        )
    else:
        s2_instruction = (
            'No company name available — write generically: '
            '"We can help businesses like yours automate follow-ups and recover leads that go cold."'
        )

    sender_desc_line = sender_desc[:120] if sender_desc else f"{sender_company} automates sales outreach via email and calls"

    prompt = f"""Write exactly 2 sentences for a cold outreach email body. Output only those 2 sentences — nothing else.

SENDER: {sender_company}
WHAT SENDER DOES: {sender_desc_line}

LEAD INFO:
{lead_context}

SENTENCE 1 — What {sender_company} does:
- Start with "{sender_company} —" then say what they do in plain English
- Use this exact description (condense to 1 sentence, keep key words): "{sender_desc_line}"
- Must mention: emails, calls, or automation specifically
- Under 20 words, humble, no jargon

SENTENCE 2 — How {sender_company} helps this lead:
- {s2_instruction}
- Under 25 words

STRICT RULES:
- Output only the 2 sentences, nothing else
- No greeting, no sign-off, no subject line, no CTA, no bullet points, no line breaks between sentences
- Never say: streamline, leverage, solutions, empower, revolutionize, boost, their company, your company
- Sound like a helpful human, not a salesperson"""

    # ── Call Groq ─────────────────────────────────────────────────────────────
    two_sentences = None

    async with httpx.AsyncClient(timeout=25.0) as client:
        for model in GROQ_MODELS:
            try:
                resp = await client.post(
                    GROQ_URL,
                    headers={
                        "Authorization": f"Bearer {settings.GROQ_API_KEY}",
                        "Content-Type":  "application/json",
                    },
                    json={
                        "model":       model,
                        "max_tokens":  120,
                        "temperature": 0.4,
                        "messages":    [{"role": "user", "content": prompt}],
                    },
                )

                if resp.status_code != 200:
                    logger.warning("Groq %s returned %s: %s", model, resp.status_code, resp.text[:200])
                    continue

                raw = resp.json()["choices"][0]["message"]["content"].strip()
                logger.debug("Groq raw output: %s", raw)

                # Drop preamble lines that have no sentence-ending punctuation
                lines         = [l.strip() for l in raw.split("\n") if l.strip()]
                content_lines = [l for l in lines if any(c in l for c in ".!?")]
                if content_lines:
                    two_sentences = " ".join(content_lines[:2])

                if two_sentences:
                    break

            except Exception as e:
                logger.warning("Groq model %s failed: %s", model, e)
                continue

    # ── Fallback — same structure, no AI ─────────────────────────────────────
    if not two_sentences:
        s1 = f"{sender_company} — {sender_desc_line}."
        if target_co and biz:
            biz_short = biz[:80].rstrip(",. ")
            s2 = f"{target_co} handles {biz_short} — we can automate your follow-ups so no lead goes cold."
        elif target_co:
            s2 = f"We can help {target_co} automate follow-ups and recover leads that go cold."
        else:
            s2 = "We can help businesses like yours automate follow-ups and recover leads that go cold."
        two_sentences = f"{s1} {s2}"

    # ── Assemble — structure is fixed here, not by AI ─────────────────────────
    CTA = (
        "Worth a quick 15-min chat to see if it fits? "
        "No pressure — feel free to ignore this if it's not relevant."
    )

    return {
        "subject": subject_line,
        "body":    f"{greeting}\n\n{two_sentences}\n\n{CTA}",
    }
