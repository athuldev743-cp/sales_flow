from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from datetime import datetime
from database import get_db
from auth_utils import (
    get_google_auth_url, exchange_google_code,
    get_google_user_info, create_access_token, get_current_user
)
from models import UserPublic
from config import settings

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

@router.get("/google")
async def google_login():
    url = get_google_auth_url()
    return {"auth_url": url}

@router.get("/google/callback")
async def google_callback(
    db=Depends(get_db),
    code: str = None,
    error: str = None
):
    if error or not code:
        return RedirectResponse(
            url=f"{settings.FRONTEND_URL}/login.html?error=cancelled"
        )

    try:
        tokens    = await exchange_google_code(code)
        user_info = await get_google_user_info(tokens["access_token"])
    except Exception:
        return RedirectResponse(
            url=f"{settings.FRONTEND_URL}/login.html?error=auth_failed"
        )

    google_id  = user_info["sub"]
    email      = user_info["email"]
    full_name  = user_info.get("name", "")
    avatar_url = user_info.get("picture", "")

    existing = await db.users.find_one({"google_id": google_id})

    if existing:
        await db.users.update_one(
            {"google_id": google_id},
            {"$set": {
                "google_access_token":  tokens["access_token"],
                "google_refresh_token": tokens.get("refresh_token"),
                "updated_at":           datetime.utcnow(),
            }}
        )
        user = await db.users.find_one({"google_id": google_id})
    else:
        new_user = {
            "email":                 email,
            "google_id":             google_id,
            "full_name":             full_name,
            "avatar_url":            avatar_url,
            "google_access_token":   tokens["access_token"],
            "google_refresh_token":  tokens.get("refresh_token"),
            "plan":                  "free",
            "instamojo_customer_id": None,
            "gmail_connected":       True,
            "whatsapp_connected":    False,
            "onboarding_complete":   False,
            "created_at":            datetime.utcnow(),
            "updated_at":            datetime.utcnow(),
        }
        result = await db.users.insert_one(new_user)
        user   = await db.users.find_one({"_id": result.inserted_id})

    access_token     = create_access_token(str(user["_id"]))
    needs_onboarding = not user.get("onboarding_complete", False)

    redirect_url = (
        f"{settings.FRONTEND_URL}/auth/callback.html"
        f"?token={access_token}"
        f"&onboarding={str(needs_onboarding).lower()}"
    )
    return RedirectResponse(url=redirect_url)

@router.get("/me", response_model=UserPublic)
async def get_me(current_user=Depends(get_current_user)):
    return serialize_user(current_user)

@router.post("/logout")
async def logout(current_user=Depends(get_current_user)):
    return {"message": "Logged out successfully"}