import httpx
import base64
import re
from datetime import datetime
from typing import Optional


GMAIL_API = "https://www.googleapis.com/gmail/v1/users/me"


async def gmail_get(path: str, access_token: str) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{GMAIL_API}{path}",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        if resp.status_code == 200:
            return resp.json()
        return None


_FOOTER_MARKERS = [
    "to unsubscribe", "unsubscribe from", "you received this",
    "this email was sent", "privacy policy", "view in browser",
    "©", "all rights reserved", "\n--\n",
]


def decode_body(data: str) -> str:
    try:
        padded  = data + "=" * (4 - len(data) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
        decoded = re.sub(r'<[^>]+>', ' ', decoded)
        decoded = re.sub(r'\s+', ' ', decoded).strip()

        lower = decoded.lower()
        cut   = len(decoded)
        for marker in _FOOTER_MARKERS:
            idx = lower.find(marker)
            if 0 < idx < cut:
                cut = idx
        decoded = decoded[:cut].strip()

        return decoded[:3000]
    except Exception:
        return ""


def extract_body(payload: dict) -> str:
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        return decode_body(data) if data else ""

    if mime_type == "text/html":
        data = payload.get("body", {}).get("data", "")
        return decode_body(data) if data else ""

    parts = payload.get("parts", [])
    for part in parts:
        text = extract_body(part)
        if text:
            return text

    return ""


def extract_header(headers: list, name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


async def get_thread_messages(access_token: str, thread_id: str) -> list:
    data = await gmail_get(f"/threads/{thread_id}?format=full", access_token)
    if not data:
        return []
    return data.get("messages", [])


async def fetch_replies(
    access_token:     str,
    campaign_emails:  list,
    sent_message_ids: list,
    our_email:        str,
    since_timestamp:  Optional[int] = None,
) -> list:
    """
    Fetch replies from Gmail using two methods:
    1. Primary:  walk threads of messages we actually sent
    2. Fallback: search inbox for messages from campaign leads

    FIX: `seen_message_ids` is shared between both methods so a message
    found by the primary method is never re-added by the fallback,
    preventing double-processing and duplicate autopilot sends.
    """
    replies          = []
    seen_message_ids = set()   # SHARED — prevents any message being returned twice

    # ── PRIMARY: walk sent-message threads ───────────────────────────────────
    for sent_msg_id in sent_message_ids:
        sent_msg = await gmail_get(
            f"/messages/{sent_msg_id}?format=metadata&metadataHeaders=threadId",
            access_token
        )
        if not sent_msg:
            continue

        thread_id = sent_msg.get("threadId")
        if not thread_id:
            continue

        thread_messages = await get_thread_messages(access_token, thread_id)

        for msg in thread_messages:
            msg_id = msg.get("id")
            if not msg_id or msg_id in seen_message_ids:
                continue
            if msg_id == sent_msg_id:
                # Skip the original message we sent
                seen_message_ids.add(msg_id)
                continue

            headers      = msg.get("payload", {}).get("headers", [])
            from_hdr     = extract_header(headers, "From")
            email_match  = re.search(r'[\w.+\-]+@[\w\-]+\.[a-zA-Z.]+', from_hdr)
            sender_email = email_match.group(0).lower() if email_match else from_hdr.lower()

            # Skip our own emails within the thread
            if our_email.lower() in sender_email:
                seen_message_ids.add(msg_id)   # mark as seen so fallback skips it too
                continue

            internal_date = int(msg.get("internalDate", 0))
            if since_timestamp and internal_date <= since_timestamp:
                seen_message_ids.add(msg_id)
                continue

            body = extract_body(msg.get("payload", {}))
            seen_message_ids.add(msg_id)

            replies.append({
                "message_id": msg_id,
                "thread_id":  thread_id,
                "from_email": sender_email,
                "from_name":  from_hdr.split("<")[0].strip().strip('"'),
                "subject":    extract_header(headers, "Subject"),
                "body":       body,
                "timestamp":  msg.get("internalDate"),
            })

    # ── FALLBACK: search inbox ────────────────────────────────────────────────
    # Only search for emails we haven't already found via the primary method
    if campaign_emails:
        sender_query = " OR ".join([f"from:{e}" for e in campaign_emails[:10]])
        query        = f"in:inbox ({sender_query})"

        if since_timestamp:
            after = since_timestamp // 1000
            query += f" after:{after}"

        search = await gmail_get(f"/messages?q={query}&maxResults=50", access_token)

        if search and "messages" in search:
            for msg_ref in search.get("messages", []):
                msg_id = msg_ref["id"]

                # FIX: skip anything already seen in the primary pass
                if msg_id in seen_message_ids:
                    continue

                msg = await gmail_get(f"/messages/{msg_id}?format=full", access_token)
                if not msg:
                    continue

                headers      = msg.get("payload", {}).get("headers", [])
                from_hdr     = extract_header(headers, "From")
                email_match  = re.search(r'[\w.+\-]+@[\w\-]+\.[a-zA-Z.]+', from_hdr)
                sender_email = email_match.group(0).lower() if email_match else from_hdr.lower()

                # Verify sender is in our campaign list
                if sender_email not in [e.lower() for e in campaign_emails]:
                    continue

                if our_email.lower() in sender_email:
                    continue

                internal_date = int(msg.get("internalDate", 0))
                if since_timestamp and internal_date <= since_timestamp:
                    continue

                seen_message_ids.add(msg_id)

                replies.append({
                    "message_id": msg_id,
                    "thread_id":  msg.get("threadId"),
                    "from_email": sender_email,
                    "from_name":  from_hdr.split("<")[0].strip().strip('"'),
                    "subject":    extract_header(headers, "Subject"),
                    "body":       extract_body(msg.get("payload", {})),
                    "timestamp":  msg.get("internalDate"),
                })

    return replies


async def send_reply(
    access_token:           str,
    thread_id:              str,
    to_email:               str,
    from_email:             str,
    from_name:              str,
    subject:                str,
    body:                   str,
    in_reply_to_message_id: str = None,
) -> dict:
    import email.mime.text
    import email.mime.multipart

    msg         = email.mime.multipart.MIMEMultipart("alternative")
    msg["To"]   = to_email
    msg["From"] = f"{from_name} <{from_email}>"
    msg["Subject"] = f"Re: {subject}" if not subject.lower().startswith("re:") else subject

    if in_reply_to_message_id:
        msg["In-Reply-To"] = in_reply_to_message_id
        msg["References"]  = in_reply_to_message_id

    plain = body
    html  = body.replace("\n", "<br>")

    msg.attach(email.mime.text.MIMEText(plain, "plain"))
    msg.attach(email.mime.text.MIMEText(
        f"<html><body style='font-family:Arial,sans-serif;font-size:14px;color:#333;'>{html}</body></html>",
        "html"
    ))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{GMAIL_API}/messages/send",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type":  "application/json",
            },
            json={"raw": raw, "threadId": thread_id}
        )

    if resp.status_code == 200:
        return {"success": True, "message_id": resp.json().get("id")}
    return {"success": False, "error": resp.text[:200]}


async def mark_as_read(access_token: str, message_id: str):
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(
            f"{GMAIL_API}/messages/{message_id}/modify",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type":  "application/json",
            },
            json={"removeLabelIds": ["UNREAD"]}
        )