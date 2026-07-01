import os

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from passlib.context import CryptContext

pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    return pwd_context.verify(plain_password, password_hash)


# --- CSRF protection -------------------------------------------------------
# Uses itsdangerous (already a project dependency) instead of adding a new
# package. Tokens are short-lived signed values tied to the same SECRET_KEY
# used for sessions, so they can't be forged without it.

def _csrf_serializer() -> URLSafeTimedSerializer:
    secret_key = os.environ["SECRET_KEY"]
    return URLSafeTimedSerializer(secret_key, salt="csrf-token")


def generate_csrf_token() -> str:
    return _csrf_serializer().dumps("csrf")


def verify_csrf_token(token: str, max_age_seconds: int = 3600) -> bool:
    if not token:
        return False
    try:
        _csrf_serializer().loads(token, max_age=max_age_seconds)
        return True
    except (BadSignature, SignatureExpired):
        return False
