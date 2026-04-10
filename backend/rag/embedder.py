"""
rag/embedder.py

Richer RAG chunks — gives the chatbot the specific context it needs to:
  - Handle objections without hallucinating
  - Sound credible about results and pricing
  - Know who NOT to pitch (wrong industries, existing tools)
  - Match the user's preferred tone
  - Use real proof points instead of generic claims
"""
import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer

_client = chromadb.PersistentClient(path="./chroma_store")
_model  = SentenceTransformer("all-MiniLM-L6-v2")


def get_user_collection(user_id: str):
    return _client.get_or_create_collection(f"user_{user_id}")


def embed_user_profile(user_id: str, profile: dict, email_template: str = ""):
    col = get_user_collection(user_id)

    def _safe(key, fallback="not provided"):
        return (profile.get(key) or "").strip() or fallback

    # ── CHUNK MAP ─────────────────────────────────────────────────────────────
    # Each chunk is a self-contained, semantically focused piece of context.
    # Keeping them separate improves retrieval precision vs one giant blob.

    chunks = {

        # WHO I AM
        "identity": (
            f"My name is {_safe('full_name')}. "
            f"I am a {_safe('sender_role', 'sales executive')} at {_safe('company_name')}. "
            f"Website: {_safe('website')}."
        ),

        # WHAT WE DO
        "company": (
            f"{_safe('company_name')} does the following: {_safe('company_description')}. "
            f"Industries we serve: {_safe('industries_served', 'B2B companies')}."
        ),

        # THE VALUE WE DELIVER
        "value_proposition": (
            f"Our core value proposition: {_safe('value_proposition')}. "
            f"The main pain point we solve: {_safe('pain_points')}."
        ),

        # PROOF — stops hallucination of fake stats
        "proof_points": (
            f"Proof points and results we have actually achieved for clients: "
            f"{_safe('proof_points', 'No specific case studies provided — do not invent statistics.')}. "
            f"IMPORTANT: Never invent numbers or claim results not listed above."
        ),

        # WHO WE SELL TO
        "audience": (
            f"Our ideal customer profile (ICP): {_safe('target_audience')}. "
            f"Best-fit company size: {_safe('company_size', 'not specified')}. "
            f"Job titles we target: {_safe('job_titles', 'not specified')}."
        ),

        # GOAL + PURPOSE
        "goal": (
            f"Purpose of outreach: {_safe('purpose')}. "
            f"Main goal: {_safe('goal')}. "
            f"Desired next step in every conversation: {_safe('desired_cta', 'book a 20-minute discovery call')}."
        ),

        # PRICING — so the chatbot never guesses
        "pricing": (
            f"How we price / what we charge: {_safe('pricing_model', 'Pricing not disclosed — do not guess or invent figures.')}. "
            f"Contract type: {_safe('contract_type', 'not specified')}."
        ),

        # OBJECTION HANDLING — reduces hallucination under pressure
        "objections": (
            f"Common objections prospects raise and how to respond: "
            f"{_safe('objection_handling', 'No specific objections provided. Acknowledge the concern and pivot to a discovery call.')}."
        ),

        # COMPETITORS — so the chatbot doesn't confuse or mention them
        "competitors": (
            f"Our main competitors are: {_safe('competitors', 'not specified')}. "
            f"How we differ: {_safe('differentiators', 'not specified')}. "
            f"IMPORTANT: Never suggest or recommend a competitor. Never say 'like [competitor]'."
        ),

        # TONE + PERSONA
        "tone": (
            f"Preferred communication tone: {_safe('tone_preference', 'warm, direct, professional — like a real human, not a bot')}. "
            f"Things to NEVER say: {_safe('never_say', 'I hope this email finds you well, circling back, just following up, as per my last email')}. "
            f"Target language/region: {_safe('language_region', 'English, India/global')}."
        ),

        # WHAT NOT TO DO — explicit guardrails embedded in memory
        "guardrails": (
            f"Hard rules for this user's AI agent: "
            f"Never mention pricing unless the lead specifically asks. "
            f"Never invent case studies or statistics not listed in proof_points. "
            f"Never contact someone who said 'not interested' or 'unsubscribe'. "
            f"Always close with one clear next step. "
            f"Keep replies under 100 words unless answering a technical question. "
            f"Additional user-defined rules: {_safe('custom_rules', 'none')}."
        ),

        "faq": (
        f"Frequently Asked Questions and verified facts: "
        f"{_safe('custom_faq', 'No specific FAQs provided. If asked a technical question not covered in other sections, acknowledge you are not 100% sure and offer to find out during a discovery call.')}."
        ),

        # EMAIL TEMPLATE
        "email_template": (
            f"The AI cold email template currently in use:\n{email_template}"
        ) if email_template else None,
    }

    # ── UPSERT ALL CHUNKS ─────────────────────────────────────────────────────
    for doc_id, text in chunks.items():
        if not text:
            continue
        embedding = _model.encode(text).tolist()
        col.upsert(
            ids=[f"{user_id}_{doc_id}"],
            embeddings=[embedding],
            documents=[text],
            metadatas=[{"type": doc_id, "user_id": user_id}],
        )


def retrieve_context(user_id: str, query: str, top_k: int = 4) -> str:
    """
    Retrieves the most relevant chunks for a given query.
    top_k=4 gives a good balance of context vs token cost.
    Always includes 'guardrails' chunk so hard rules are never missed.
    """
    col    = get_user_collection(user_id)
    q_emb  = _model.encode(query).tolist()
    results = col.query(query_embeddings=[q_emb], n_results=top_k)
    docs   = results.get("documents", [[]])[0]

    # Always inject guardrails regardless of query relevance
    try:
        guardrail_result = col.get(ids=[f"{user_id}_guardrails"])
        guardrail_docs   = guardrail_result.get("documents", [])
        if guardrail_docs and guardrail_docs[0] not in docs:
            docs.append(guardrail_docs[0])
    except Exception:
        pass

    return "\n\n".join(docs)