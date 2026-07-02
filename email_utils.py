import requests

import config


RESEND_EMAILS_URL = "https://api.resend.com/emails"


def _sender() -> str:
    if config.EMAIL_FROM_NAME:
        return f"{config.EMAIL_FROM_NAME} <{config.EMAIL_FROM}>"
    return config.EMAIL_FROM


def send_email(to_email: str, subject: str, body: str) -> bool:
    if not config.email_configured:
        print(f"[email disabled] {subject} for {to_email}: {body}")
        return False

    payload = {
        "from": _sender(),
        "to": [to_email],
        "subject": subject,
        "text": body,
    }
    headers = {
        "Authorization": f"Bearer {config.RESEND_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(RESEND_EMAILS_URL, json=payload, headers=headers, timeout=10)
        if response.status_code >= 400:
            print(
                f"[email send failed] {subject} for {to_email}: "
                f"resend_status={response.status_code} body={response.text[:500]!r}"
            )
            return False
        return True
    except Exception as exc:
        print(f"[email send failed] {subject} for {to_email}: {exc!r}")
        return False


def send_verification_email(to_email: str, verification_link: str) -> bool:
    body = (
        "Welcome to BashOps Radar.\n\n"
        "Verify your email address to activate your account:\n"
        f"{verification_link}\n\n"
        "If you did not create this account, you can ignore this email."
    )
    return send_email(to_email, "Verify your BashOps Radar account", body)


def send_password_reset_email(to_email: str, reset_link: str) -> bool:
    body = (
        "Use this link to reset your BashOps Radar password:\n"
        f"{reset_link}\n\n"
        "This link expires in 1 hour. If you did not request it, you can ignore this email."
    )
    return send_email(to_email, "Reset your BashOps Radar password", body)
