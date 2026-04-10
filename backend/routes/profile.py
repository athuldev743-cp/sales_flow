from fastapi import APIRouter, Depends, HTTPException
from datetime import datetime
from bson import ObjectId
from database import get_db
from auth_utils import get_current_user
from models import ProfileSubmit, EmailTemplateUpdate, UserPublic
from config import settings
import httpx
from rag.embedder import embed_user_profile

router = APIRouter()


def serialize_user(user: dict) -> UserPublic:
    return UserPublic(
        id=str(user["_id"]),
        email=user["email"],
        full_name=user["full_name"],
        avatar_url=user.get("avatar_url"),
        profile=user.get("profile"),
        ai_email_template=user.get("ai_email_template"),
        plan=user.get("plan", "free"),
        gmail_connected=user.get("gmail_connected", False),
        whatsapp_connected=user.get("whatsapp_connected", False),
        onboarding_complete=user.get("onboarding_complete", False),
    )


async def call_groq(prompt: str) -> str:
    models = [
        "moonshotai/kimi-k2-instruct",
        "llama-3.1-8b-instant",
    ]

    print(
        f"GROQ KEY: {settings.GROQ_API_KEY[:15]}...{settings.GROQ_API_KEY[-4:]}")

    async with httpx.AsyncClient(timeout=30.0) as client:
        for model in models:
            try:
                resp = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.GROQ_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "max_tokens": 600,
                        "messages": [{"role": "user", "content": prompt}]
                    }
                )
                print(f"GROQ {model} status: {resp.status_code}")
                if resp.status_code == 200:
                    return resp.json()["choices"][0]["message"]["content"]
                else:
                    print(f"GROQ {model} error: {resp.text[:200]}")
            except Exception as e:
                print(f"GROQ {model} exception: {str(e)}")
                continue

    return None  # all models failed


async def generate_email_template(profile: ProfileSubmit) -> str:
    # ── THE NEW PEER-TO-PEER PROMPT ──
    # Updated to align with your new template's specific tone and structure
    prompt = f"""You are {profile.full_name}, a founder reaching out to another founder. 
Tone: Minimalist, direct, and helpful. 100% human.

Context:
- Sender: {profile.full_name} (@ {profile.company_name})
- Solving: {profile.company_description}
- For: {profile.target_audience}
- Objective: {profile.goal}

Format:
SUBJECT: Quick question for {{lead_company}}

Hi {{lead_name}},

[Sentence 1: Natural observation about {{lead_company}} space/pipeline/outreach]
[Sentence 2: How {profile.company_name} helps {profile.target_audience} automate {profile.purpose} to achieve {profile.goal}]
[Sentence 3: The benefit - focusing on closing/results rather than manual work]
[Sentence 4: CTA: 'Would a 20-minute call this week make sense?']

RULES:
- NO signature (The system adds it automatically).
- NO "Series-B", "Pilot slots", or "I hope this finds you well".
- Total body: Under 80 words.
- ONLY use {{lead_name}} and {{lead_company}} as placeholders."""

    result = await call_groq(prompt)
    if result:
        return result

    # ── UPDATED DYNAMIC FALLBACK ──
    # This now reflects the specific structure you provided
    
    subject_fallback = f"Quick question for {{lead_company}}"

    body_fallback = (
        f"Hi {{lead_name}},\n\n"
        f"I came across {{lead_company}} and noticed you're in a space where "
        f"consistent outbound usually makes or breaks pipeline.\n\n"
        f"At {profile.company_name}, we help {profile.target_audience} automate their outreach "
        f"so their reps spend time closing, not prospecting — without adding headcount or new tools.\n\n"
        f"Would a 20-minute call this week make sense to see if we can help?"
    )

    return f"SUBJECT: {subject_fallback}\n\n{body_fallback}"


@router.get("/test-ai")
async def test_ai(current_user=Depends(get_current_user)):
    result = await call_groq("Write one sentence about sales automation.")
    return {
        "groq_key_preview": f"{settings.GROQ_API_KEY[:15]}...{settings.GROQ_API_KEY[-4:]}",
        "result": result,
        "success": result is not None
    }


@router.post("/submit")
async def submit_profile(
    data: ProfileSubmit,
    current_user=Depends(get_current_user),
    db=Depends(get_db)
):
    try:
        email_template = await generate_email_template(data)
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"AI generation failed: {str(e)}")

    profile_dict = data.model_dump()

    # 1. Update the database first
    await db.users.update_one(
        {"_id": current_user["_id"]},
        {"$set": {
            "profile":             profile_dict,
            "ai_email_template":   email_template,
            "onboarding_complete": True,
            "full_name":           data.full_name,
            "updated_at":          datetime.utcnow(),
        }}
    )

    # 2. Add the RAG embedding logic here
    try:
        embed_user_profile(
            user_id=str(current_user["_id"]),
            profile=profile_dict,
            email_template=email_template
        )
    except Exception as e:
        print(f"RAG embed failed (non-fatal): {e}")

    # 3. Return response
    return {
        "message":           "Profile saved",
        "ai_email_template": email_template,
    }

@router.post("/regenerate-email")
async def regenerate_email(
    current_user=Depends(get_current_user),
    db=Depends(get_db)
):
    profile = current_user.get("profile")
    if not profile:
        raise HTTPException(
            status_code=400, detail="Complete your profile first")

    profile_obj = ProfileSubmit(**profile)
    email_template = await generate_email_template(profile_obj)

    # 1. Update the database
    await db.users.update_one(
        {"_id": ObjectId(current_user["_id"])},
        {"$set": {
            "ai_email_template": email_template,
            "updated_at":        datetime.utcnow(),
        }}
    )

    # 2. Refresh RAG chunks so the memory stays fresh
    try:
        embed_user_profile(
            user_id=str(current_user["_id"]),
            profile=profile,  # Use existing profile dict
            email_template=email_template
        )
    except Exception as e:
        print(f"RAG re-embed failed (non-fatal): {e}")

    return {"ai_email_template": email_template}
