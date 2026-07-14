import requests
from typing import Optional

import config


RESEND_EMAILS_URL = "https://api.resend.com/emails"


def _masked_recipient(to_email: str) -> str:
    local, separator, domain = (to_email or "").strip().partition("@")
    if not separator or not local or not domain:
        return "invalid-recipient"
    return f"{local[0]}***@{domain.casefold()}"


def _sender() -> str:
    if config.EMAIL_FROM_NAME:
        return f"{config.EMAIL_FROM_NAME} <{config.EMAIL_FROM}>"
    return config.EMAIL_FROM


def _email_shell(headline: str, body_html: str, cta_text: str, cta_url: str) -> str:
    return f"""<!doctype html>
<html>
<body style="margin:0;background:#0b0e14;color:#f8fafc;font-family:Arial,sans-serif;">
  <div style="max-width:620px;margin:0 auto;padding:32px 20px;">
    <div style="border:1px solid #2c3440;border-radius:18px;background:#171c24;padding:28px;">
      <div style="color:#f59e0b;font-weight:800;font-size:14px;margin-bottom:18px;">&gt;_ BashOps Radar</div>
      <h1 style="font-size:28px;line-height:1.2;margin:0 0 14px;color:#f8fafc;">{headline}</h1>
      <div style="font-size:16px;line-height:1.65;color:#cbd5e1;">{body_html}</div>
      <p style="margin:26px 0;">
        <a href="{cta_url}" style="display:inline-block;background:#f59e0b;color:#111827;text-decoration:none;font-weight:800;border-radius:10px;padding:12px 18px;">{cta_text}</a>
      </p>
      <p style="font-size:13px;line-height:1.6;color:#94a3b8;">If the button does not work, copy and paste this link into your browser:<br>
      <a href="{cta_url}" style="color:#f59e0b;">{cta_url}</a></p>
      <p style="font-size:13px;color:#94a3b8;margin-top:24px;">Need help? Contact support@bashops.site.</p>
    </div>
  </div>
</body>
</html>"""


def send_email(to_email: str, subject: str, body: str, html_body: Optional[str] = None) -> bool:
    recipient = _masked_recipient(to_email)
    if not config.email_configured:
        print(f"[email disabled] subject={subject!r} recipient={recipient}")
        return False

    payload = {
        "from": _sender(),
        "to": [to_email],
        "subject": subject,
        "text": body,
    }
    if html_body:
        payload["html"] = html_body
    headers = {
        "Authorization": f"Bearer {config.RESEND_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(RESEND_EMAILS_URL, json=payload, headers=headers, timeout=10)
        if response.status_code >= 400:
            print(
                f"[email send failed] subject={subject!r} recipient={recipient} "
                f"resend_status={response.status_code}"
            )
            return False
        return True
    except Exception as exc:
        print(
            f"[email send failed] subject={subject!r} recipient={recipient} "
            f"error={exc.__class__.__name__}"
        )
        return False


def send_verification_email(to_email: str, verification_link: str) -> bool:
    body = (
        "Welcome to BashOps Radar.\n\n"
        "Verify your BashOps Radar account:\n"
        f"{verification_link}\n\n"
        "After verification you can analyze GitHub repositories, find high-probability issues, "
        "track Proof-of-Work opportunities, and upgrade later for founder outreach.\n\n"
        "Need help? Contact support@bashops.site.\n\n"
        "If you did not create this account, you can ignore this email."
    )
    html_body = _email_shell(
        "Verify your BashOps Radar account",
        (
            "<p>Confirm your email to activate your workspace.</p>"
            "<ul>"
            "<li>Analyze GitHub repositories</li>"
            "<li>Find high-probability issues</li>"
            "<li>Track Proof-of-Work opportunities</li>"
            "<li>Upgrade later for founder outreach</li>"
            "</ul>"
        ),
        "Verify Email",
        verification_link,
    )
    return send_email(to_email, "Verify your BashOps Radar account", body, html_body)


def send_password_reset_email(to_email: str, reset_link: str) -> bool:
    body = (
        "Use this link to reset your BashOps Radar password:\n"
        f"{reset_link}\n\n"
        "This link expires in 1 hour. If you did not request it, you can ignore this email."
    )
    html_body = _email_shell(
        "Reset your BashOps Radar password",
        (
            "<p>Use the button below to set a new password.</p>"
            "<p><strong>This link expires in 1 hour.</strong></p>"
            "<p>If you did not request this, you can ignore this email.</p>"
        ),
        "Reset Password",
        reset_link,
    )
    return send_email(to_email, "Reset your BashOps Radar password", body, html_body)
