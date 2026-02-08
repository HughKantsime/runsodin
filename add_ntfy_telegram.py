#!/usr/bin/env python3
"""
Add ntfy and Telegram webhook types to O.D.I.N.
Run on server: python3 add_ntfy_telegram.py

This extends the existing webhook system to support:
- ntfy (simple HTTP POST to ntfy.sh or self-hosted)
- Telegram (Bot API)

Users configure these like any webhook — name, URL, type.
The test and dispatch functions handle formatting per type.
"""

MAIN_PY = "/opt/printfarm-scheduler/backend/main.py"

with open(MAIN_PY, "r") as f:
    content = f.read()

changes = 0

# ============================================================
# 1. Update the test_webhook endpoint to handle ntfy + telegram
# ============================================================

old_test = '''    try:
        import httpx
        
        if webhook["webhook_type"] == "discord":
            payload = {
                "embeds": [{
                    "title": "🖨️ O.D.I.N. Test",
                    "description": "Webhook connection successful!",
                    "color": 0x00ff00,
                    "footer": {"text": "O.D.I.N."}
                }]
            }
        else:  # slack
            payload = {
                "blocks": [
                    {"type": "header", "text": {"type": "plain_text", "text": "🖨️ O.D.I.N. Test"}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": "Webhook connection successful!"}}
                ]
            }
        
        resp = httpx.post(webhook["url"], json=payload, timeout=10)
        
        if resp.status_code in (200, 204):
            return {"success": True, "message": "Test message sent"}
        else:
            return {"success": False, "message": f"Failed: HTTP {resp.status_code}"}
    
    except Exception as e:
        return {"success": False, "message": str(e)}'''

new_test = '''    try:
        import httpx
        
        wtype = webhook["webhook_type"]
        
        if wtype == "discord":
            payload = {
                "embeds": [{
                    "title": "🖨️ O.D.I.N. Test",
                    "description": "Webhook connection successful!",
                    "color": 0xd97706,
                    "footer": {"text": "O.D.I.N."}
                }]
            }
            resp = httpx.post(webhook["url"], json=payload, timeout=10)
        
        elif wtype == "slack":
            payload = {
                "blocks": [
                    {"type": "header", "text": {"type": "plain_text", "text": "🖨️ O.D.I.N. Test"}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": "Webhook connection successful!"}}
                ]
            }
            resp = httpx.post(webhook["url"], json=payload, timeout=10)
        
        elif wtype == "ntfy":
            # ntfy: URL is the topic endpoint (e.g., https://ntfy.sh/my-printfarm)
            resp = httpx.post(
                webhook["url"],
                content="Webhook connection successful!",
                headers={
                    "Title": "O.D.I.N. Test",
                    "Priority": "default",
                    "Tags": "white_check_mark,printer",
                },
                timeout=10
            )
        
        elif wtype == "telegram":
            # Telegram: URL format is https://api.telegram.org/bot<TOKEN>/sendMessage
            # User stores just the bot token + chat_id in the URL as:
            #   bot_token|chat_id  (we parse and construct the API call)
            # OR they can store the full URL with chat_id as a query param
            url = webhook["url"]
            if "|" in url:
                # Format: bot_token|chat_id
                bot_token, chat_id = url.split("|", 1)
                api_url = f"https://api.telegram.org/bot{bot_token.strip()}/sendMessage"
            else:
                # Assume full URL, extract chat_id from stored data
                # Fallback: treat URL as bot token, chat_id from name field
                api_url = f"https://api.telegram.org/bot{url.strip()}/sendMessage"
                chat_id = webhook.get("name", "").split("|")[-1] if "|" in webhook.get("name", "") else ""
            
            resp = httpx.post(
                api_url,
                json={
                    "chat_id": chat_id.strip(),
                    "text": "🖨️ *O.D.I.N. Test*\\nWebhook connection successful!",
                    "parse_mode": "Markdown"
                },
                timeout=10
            )
        
        else:
            # Generic webhook — POST JSON
            payload = {
                "event": "test",
                "source": "odin",
                "message": "Webhook connection successful!"
            }
            resp = httpx.post(webhook["url"], json=payload, timeout=10)
        
        if resp.status_code in (200, 204):
            return {"success": True, "message": "Test message sent"}
        else:
            return {"success": False, "message": f"Failed: HTTP {resp.status_code} - {resp.text[:200]}"}
    
    except Exception as e:
        return {"success": False, "message": str(e)}'''

if old_test in content:
    content = content.replace(old_test, new_test)
    changes += 1
    print("✓ Updated test_webhook to support ntfy + telegram")

# ============================================================
# 2. Add send_webhook_alert utility function for dispatch
# ============================================================
# This function is called by the alert dispatcher to fan out to webhooks.
# Check if there's already a webhook dispatch function.

WEBHOOK_DISPATCH = '''

# ============================================================
# Webhook Alert Dispatch (v0.18.0 — ntfy + telegram support)
# ============================================================

def _dispatch_to_webhooks(db, alert_type_value: str, title: str, message: str, severity: str):
    """Send alert to all matching enabled webhooks."""
    import httpx
    import threading
    
    rows = db.execute(text("SELECT * FROM webhooks WHERE is_enabled = 1")).fetchall()
    
    for row in rows:
        wh = dict(row._mapping)
        
        # Check if this webhook subscribes to this alert type
        alert_types = wh.get("alert_types")
        if alert_types:
            try:
                types_list = json.loads(alert_types) if isinstance(alert_types, str) else alert_types
                if alert_type_value not in types_list and "all" not in types_list:
                    continue
            except (json.JSONDecodeError, TypeError):
                pass
        
        wtype = wh["webhook_type"]
        url = wh["url"]
        
        severity_colors = {"critical": 0xef4444, "warning": 0xf59e0b, "info": 0x3b82f6}
        severity_emoji = {"critical": "🔴", "warning": "🟡", "info": "🔵"}
        emoji = severity_emoji.get(severity, "🔵")
        color = severity_colors.get(severity, 0x3b82f6)
        
        def _send(wtype=wtype, url=url):
            try:
                if wtype == "discord":
                    httpx.post(url, json={
                        "embeds": [{
                            "title": f"{emoji} {title}",
                            "description": message or "",
                            "color": color,
                            "footer": {"text": "O.D.I.N."}
                        }]
                    }, timeout=10)
                
                elif wtype == "slack":
                    httpx.post(url, json={
                        "blocks": [
                            {"type": "header", "text": {"type": "plain_text", "text": f"{emoji} {title}"}},
                            {"type": "section", "text": {"type": "mrkdwn", "text": message or ""}}
                        ]
                    }, timeout=10)
                
                elif wtype == "ntfy":
                    priority_map = {"critical": "urgent", "warning": "high", "info": "default"}
                    httpx.post(url, content=message or title, headers={
                        "Title": title,
                        "Priority": priority_map.get(severity, "default"),
                        "Tags": "printer",
                    }, timeout=10)
                
                elif wtype == "telegram":
                    if "|" in url:
                        bot_token, chat_id = url.split("|", 1)
                        api_url = f"https://api.telegram.org/bot{bot_token.strip()}/sendMessage"
                    else:
                        api_url = f"https://api.telegram.org/bot{url.strip()}/sendMessage"
                        chat_id = ""
                    
                    if chat_id:
                        httpx.post(api_url, json={
                            "chat_id": chat_id.strip(),
                            "text": f"{emoji} *{title}*\\n{message or ''}",
                            "parse_mode": "Markdown"
                        }, timeout=10)
                
                else:
                    httpx.post(url, json={
                        "event": alert_type_value,
                        "title": title,
                        "message": message or "",
                        "severity": severity
                    }, timeout=10)
            
            except Exception as e:
                log.error(f"Webhook dispatch failed ({wtype}): {e}")
        
        thread = threading.Thread(target=_send, daemon=True)
        thread.start()

'''

if "_dispatch_to_webhooks" not in content:
    # Insert before the cameras endpoint
    INSERT_BEFORE = '@app.get("/api/cameras", tags=["Cameras"])'
    if INSERT_BEFORE in content:
        content = content.replace(INSERT_BEFORE, WEBHOOK_DISPATCH + INSERT_BEFORE)
        changes += 1
        print("✓ Added _dispatch_to_webhooks function")

# ============================================================
# 3. Wire webhook dispatch into the alert dispatcher
# ============================================================
# We need to call _dispatch_to_webhooks from alert_dispatcher.py
# The cleanest way: add it to the end of dispatch_alert in alert_dispatcher.py

DISPATCHER_PY = "/opt/printfarm-scheduler/backend/alert_dispatcher.py"

with open(DISPATCHER_PY, "r") as f:
    disp_content = f.read()

old_dispatch_end = '''    logger.info(f"Dispatched {alert_type.value} alert to {alerts_created} users: {title}")
    return alerts_created'''

new_dispatch_end = '''    # Dispatch to webhooks (ntfy, telegram, discord, slack)
    try:
        from main import _dispatch_to_webhooks
        _dispatch_to_webhooks(db, alert_type.value, title, message, severity.value)
    except ImportError:
        pass
    except Exception as e:
        logger.error(f"Webhook dispatch error: {e}")
    
    logger.info(f"Dispatched {alert_type.value} alert to {alerts_created} users: {title}")
    return alerts_created'''

if "_dispatch_to_webhooks" not in disp_content:
    if old_dispatch_end in disp_content:
        disp_content = disp_content.replace(old_dispatch_end, new_dispatch_end)
        with open(DISPATCHER_PY, "w") as f:
            f.write(disp_content)
        changes += 1
        print("✓ Wired webhook dispatch into alert_dispatcher.py")
    else:
        print("✗ Could not find dispatch_alert return in alert_dispatcher.py")

# Write main.py
if changes > 0:
    with open(MAIN_PY, "w") as f:
        f.write(content)
    print(f"\n✅ Applied {changes} changes for ntfy + telegram support")
else:
    print("\n⚠ No changes applied")
