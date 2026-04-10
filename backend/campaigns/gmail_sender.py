import httpx
import base64
import re
import uuid
import email.mime.multipart
from email.mime.text import MIMEText
from email.utils import formatdate


GMAIL_SEND_URL    = "https://www.googleapis.com/gmail/v1/users/me/messages/send"
GMAIL_API         = "https://www.googleapis.com/gmail/v1/users/me"
GMAIL_REFRESH_URL = "https://oauth2.googleapis.com/token"

_FORBIDDEN_COMPANIES = {"your company", "none", "unknown", "—", "company", "undefined", "null", ""}


async def refresh_access_token(refresh_token: str, client_id: str, client_secret: str) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.post(GMAIL_REFRESH_URL, data={
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token,
            "client_id":     client_id,
            "client_secret": client_secret,
        })
        if resp.status_code == 200:
            return resp.json().get("access_token")
        raise Exception(f"Token refresh failed: {resp.text}")


def _resolve_display_name(lead_name: str | None, to_email: str) -> str:
    if lead_name and lead_name.strip() and lead_name.strip().lower() not in ("there", "none", "unknown"):
        return lead_name.strip().split()[0].title()
    prefix = to_email.split("@")[0]
    clean  = re.sub(r"[^a-zA-Z]", " ", prefix).strip()
    return clean.split()[0].title() if len(clean) > 1 else "there"


def build_email(
    to_email:          str,
    from_email:        str,
    from_name:         str,
    subject:           str,
    body:              str,
    unsubscribe_token: str  | None = None,
    user_company:      str  | None = None,
    lead_company:      str  | None = None,
    brand_color:       str         = "#7c6dfa",
    lead_name:         str  | None = None,
) -> str:

    # ── 1. Resolve names ──────────────────────────────────────────────────────
    display_name = _resolve_display_name(lead_name, to_email)

    clean_lead_company = (lead_company or "").strip()
    if clean_lead_company.lower() in _FORBIDDEN_COMPANIES:
        clean_lead_company = ""

    # ── 2. Replace any remaining {placeholders} (safety net) ─────────────────
    body    = body.replace("{lead_name}",    display_name)
    body    = body.replace("{lead_company}", clean_lead_company)
    subject = (subject or "").replace("{lead_name}",    display_name)
    subject = subject.replace("{lead_company}", clean_lead_company)

    # ── 3. Sanitise body — strip any trailing leaked sender name/company ─────
    # The AI sometimes appends the sender name or company as a sign-off
    # even when told not to. Strip any trailing lines that are just the name.
    _bad_endings = {
        (user_company or "").strip().lower(),
        (from_name or "").strip().lower(),
    } - {""}
    body_lines = body.strip().split("\n")
    while body_lines:
        last = body_lines[-1].strip().lower()
        if last in _bad_endings or last in {"karmasauto", "salesflow", "regards", "thanks"}:
            body_lines.pop()
        else:
            break
    body = "\n".join(body_lines).strip()

    # ── 4. Format body for HTML ───────────────────────────────────────────────
    html_content = body.strip().replace("\n", "<br>").replace("\\n", "<br>")

    # ── 5. Sender company display ─────────────────────────────────────────────
    display_user_company = (user_company or "").strip()
    if display_user_company.lower() in _FORBIDDEN_COMPANIES:
        display_user_company = ""

    # ── 5. Build MIME object ──────────────────────────────────────────────────
    msg = email.mime.multipart.MIMEMultipart("alternative")
    msg["To"]         = to_email
    msg["From"]       = f"{from_name} <{from_email}>"
    msg["Subject"]    = subject
    msg["Date"]       = formatdate(localtime=True)
    msg["Message-ID"] = f"<{uuid.uuid4()}@{from_email.split('@')[1]}>"
    if unsubscribe_token:
        msg["List-Unsubscribe"] = f"<mailto:{from_email}?subject=unsubscribe_{unsubscribe_token}>"

    # ── 6. HTML template ──────────────────────────────────────────────────────
    # NOTE: The AI body already ends cleanly (no sender name).
    # The signature block below adds from_name + company ONCE only.
    html_template = f"""
<html>
<body style="margin:0;padding:0;background-color:#f4f7ff;font-family:'Segoe UI',Tahoma,sans-serif;">
  <table width="100%" border="0" cellspacing="0" cellpadding="0"
         style="background-color:#f4f7ff;padding:30px 10px;">
    <tr><td align="center">
      <table width="100%" border="0" cellspacing="0" cellpadding="0"
             style="max-width:600px;background-color:#ffffff;border-radius:16px;
                    overflow:hidden;box-shadow:0 4px 12px rgba(0,0,0,0.1);">

        <tr><td style="background:linear-gradient(90deg,{brand_color} 0%,#a855f7 100%);
                        height:6px;"></td></tr>

        <tr><td style="padding:40px;text-align:left;">
          {f'<div style="color:{brand_color};font-size:11px;font-weight:800;letter-spacing:1px;text-transform:uppercase;margin-bottom:20px;">{display_user_company}</div>' if display_user_company else ''}

          <div style="color:#475569;line-height:1.8;font-size:16px;margin-bottom:30px;">
            {html_content}
          </div>

          <div style="border-top:1px solid #f1f5f9;padding-top:25px;">
            <p style="margin:0;font-weight:700;color:#0f172a;font-size:16px;">{from_name}</p>
            {f'<p style="margin:4px 0 0 0;color:{brand_color};font-size:14px;font-weight:500;">{display_user_company}</p>' if display_user_company else ''}
          </div>
        </td></tr>

        <tr><td style="padding:20px;text-align:center;background-color:#f8fafc;">
          <p style="margin:0;font-size:12px;color:#94a3b8;">
            &copy; 2026 {display_user_company or "SalesFlow"}. All rights reserved.<br>
            <a href="#" style="color:{brand_color};text-decoration:none;">Manage preferences</a>
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""

    # ── 7. Attach & encode ────────────────────────────────────────────────────
    msg.attach(MIMEText(body.strip(), "plain"))
    msg.attach(MIMEText(html_template, "html"))
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")


async def get_message_header(access_token: str, message_id: str, header_name: str) -> str:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{GMAIL_API}/messages/{message_id}?format=metadata&metadataHeaders={header_name}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code != 200:
            return ""
        for h in resp.json().get("payload", {}).get("headers", []):
            if h.get("name", "").lower() == header_name.lower():
                return h.get("value", "")
    return ""


async def send_gmail(
    access_token:      str,
    to_email:          str,
    from_email:        str,
    from_name:         str,
    subject:           str,
    body:              str,
    unsubscribe_token: str  | None = None,
    user_company:      str  | None = None,
    lead_company:      str  | None = None,
    brand_color:       str  | None = None,
    lead_name:         str  | None = None,
) -> dict:

    raw = build_email(
        to_email=to_email,
        from_email=from_email,
        from_name=from_name,
        subject=subject,
        body=body,
        unsubscribe_token=unsubscribe_token,
        user_company=user_company,
        lead_company=lead_company,
        brand_color=brand_color or "#7c6dfa",
        lead_name=lead_name,
    )

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            GMAIL_SEND_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type":  "application/json",
            },
            json={"raw": raw},
        )

    if resp.status_code == 200:
        data      = resp.json()
        gmail_id  = data.get("id")
        thread_id = data.get("threadId")
        msg_id_header = await get_message_header(access_token, gmail_id, "Message-ID")
        return {
            "success":                 True,
            "message_id":              gmail_id,
            "thread_id":               thread_id,
            "gmail_message_id_header": msg_id_header,
            "error":                   None,
        }
    elif resp.status_code == 401:
        return {"success": False, "message_id": None, "thread_id": None,
                "gmail_message_id_header": None, "error": "token_expired"}
    else:
        error = resp.json().get("error", {}).get("message", resp.text[:100])
        return {"success": False, "message_id": None, "thread_id": None,
                "gmail_message_id_header": None, "error": error}