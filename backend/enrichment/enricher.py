import httpx
import json
import logging
from config import settings

logger = logging.getLogger(__name__)


async def enrich_lead_with_ai(
    company_name: str,
    business_details: str,
    city: str = "",
    state: str = "",
    existing_email: str = "",
    existing_website: str = "",
) -> dict:
    """
    Use Groq (llama-3.1-8b-instant) to enrich lead data.
    Returns a dict with industry, company_size, pain_points,
    enriched_description, suggested_subject, and confidence.
    """

    location = ", ".join(filter(None, [city, state]))
    raw_details = (business_details or "")[:600]

    prompt = f"""You are a B2B sales data enrichment assistant.

Company name : {company_name}
Location     : {location or "India"}
Description  : {raw_details or "Not available"}
Email        : {existing_email or "Unknown"}
Website      : {existing_website or "Unknown"}

Return ONLY a valid JSON object (no markdown, no extra text) with these exact keys:
{{
  "enriched_description": "2-sentence professional description of what this business likely does",
  "industry": "one of: Manufacturing, Retail, IT Services, Healthcare, Education, Logistics, Finance, Real Estate, Food & Beverage, Textile, Pharma, Agriculture, Hospitality, Construction, Other",
  "company_size": "one of: 1-10, 11-50, 51-200, 200+",
  "pain_points": ["pain1", "pain2", "pain3"],
  "suggested_subject": "a short compelling cold-email subject line under 60 chars",
  "confidence": 0.0
}}

confidence should be 0.0-1.0 based on how much real info you had to work with."""

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.GROQ_API_KEY}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":       "llama-3.1-8b-instant",
                    "messages":    [{"role": "user", "content": prompt}],
                    "temperature": 0.2,
                    "max_tokens":  512,
                },
            )
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"].strip()

            # Strip markdown fences if present
            if text.startswith("```"):
                parts = text.split("```")
                text = parts[1] if len(parts) > 1 else parts[0]
                if text.startswith("json"):
                    text = text[4:]
            text = text.strip()

            return json.loads(text)

    except Exception as e:
        logger.warning("Enrichment failed for %s: %s", company_name, e)
        return {
            "enriched_description": raw_details[:200] or "",
            "industry":             "Other",
            "company_size":         "Unknown",
            "pain_points":          [],
            "suggested_subject":    f"Quick question for {company_name}",
            "confidence":           0.0,
            "error":                str(e),
        }