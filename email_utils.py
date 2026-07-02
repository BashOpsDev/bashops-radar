import smtplib
from email.message import EmailMessage

import config


def _sender() -> str:
    if config.SMTP_FROM_NAME:
        return f"{config.SMTP_FROM_NAME} <{config.SMTP_FROM_EMAIL}>"
    return config.SMTP_FROM_EMAIL


def send_email(to_email: str, subject: str, body: str) -> bool:
    if not config.smtp_configured:
        print(f"[email disabled] {subject} for {to_email}: {body}")
        return False

    message = EmailMessage()
    message["From"] = _sender()
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(body)

    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as smtp:
            smtp.starttls()
            smtp.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
            smtp.send_message(message)
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
