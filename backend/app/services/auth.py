"""Password hashing, JWT issue/verify, and user lookup.

Choices worth defending:

* **bcrypt** for passwords, with a per-password salt handled by the library. It
  is deliberately slow, which is the entire point: a stolen table should be
  expensive to attack offline.
* **A dummy hash check for unknown emails.** Without it, a missing user returns
  measurably faster than a wrong password, and login becomes a user-enumeration
  oracle. The response text is identical for both cases too.
* **Short-lived HS256 tokens** carrying the role. The role is re-read from the
  database on every request anyway (see `app/api/deps.py`), so a token cannot
  outlive a revoked account or keep a privilege that has since been removed.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import User, UserRole

#: bcrypt silently truncates anything longer, so it is rejected up front.
MAX_PASSWORD_BYTES = 72

#: Compared against when no user matches, purely to keep the timing flat.
_DUMMY_HASH = bcrypt.hashpw(b"dummy-password-for-constant-time", bcrypt.gensalt())


class AuthError(Exception):
    """Base class for authentication failures."""


class InvalidCredentialsError(AuthError):
    """Wrong email, wrong password, or a deactivated account."""


class InvalidTokenError(AuthError):
    """The token is missing, malformed, expired, or not ours."""


class PasswordTooLongError(AuthError):
    """Longer than bcrypt can actually hash."""


def hash_password(password: str) -> str:
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise PasswordTooLongError(
            f"password must be at most {MAX_PASSWORD_BYTES} bytes; bcrypt would "
            "silently truncate anything longer"
        )
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    encoded = password.encode("utf-8")[:MAX_PASSWORD_BYTES]
    try:
        return bcrypt.checkpw(encoded, password_hash.encode("utf-8"))
    except ValueError:
        # A malformed hash in the database is not a reason to let anyone in.
        return False


def authenticate_user(session: Session, *, email: str, password: str) -> User:
    """Return the user, or raise. Identical failure for every reason, by design."""
    user = session.scalar(select(User).where(User.email == email.strip().lower()))

    if user is None:
        # Spend the same time as a real check so absence is not detectable.
        bcrypt.checkpw(password.encode("utf-8")[:MAX_PASSWORD_BYTES], _DUMMY_HASH)
        raise InvalidCredentialsError("incorrect email or password")

    if not verify_password(password, user.password_hash):
        raise InvalidCredentialsError("incorrect email or password")

    if not user.is_active:
        raise InvalidCredentialsError("incorrect email or password")

    return user


def create_access_token(user: User, settings: Settings) -> tuple[str, int]:
    """Return `(token, expires_in_seconds)`."""
    expires_in = settings.access_token_expire_minutes * 60
    issued_at = datetime.now(UTC)
    claims = {
        "sub": str(user.id),
        "email": user.email,
        "role": user.role.value,
        "iat": issued_at,
        "exp": issued_at + timedelta(seconds=expires_in),
    }
    token = jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_in


def decode_access_token(token: str, settings: Settings) -> dict[str, Any]:
    try:
        return jwt.decode(
            token,
            settings.jwt_secret,
            # Pinning the algorithm is what stops an attacker swapping in
            # "none", or downgrading an RS256 setup to HS256 with the public key.
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise InvalidTokenError("could not validate credentials") from exc


def get_user_by_id(session: Session, user_id: str) -> User | None:
    try:
        parsed = uuid.UUID(user_id)
    except (ValueError, AttributeError, TypeError):
        return None
    return session.get(User, parsed)


def create_user(
    session: Session, *, email: str, password: str, role: UserRole
) -> User:
    """Create a user with a hashed password. Flushes, does not commit."""
    user = User(
        email=email.strip().lower(),
        password_hash=hash_password(password),
        role=role,
    )
    session.add(user)
    session.flush()
    return user
