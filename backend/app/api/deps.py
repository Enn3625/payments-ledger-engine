"""Authentication and role dependencies.

The user is re-loaded from the database on every request rather than trusted
from the token body. A JWT is a claim about the past; the database is the
present. Deactivate an account or demote an admin and the change takes effect
on their next request, not when their token happens to expire.
"""

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_session
from app.models import User, UserRole
from app.services.auth import InvalidTokenError, decode_access_token, get_user_by_id

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)

CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_current_user(
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    token: Annotated[str | None, Depends(oauth2_scheme)] = None,
) -> User:
    """Any authenticated, active user. This is the read-access floor."""
    if not token:
        raise CREDENTIALS_EXCEPTION

    try:
        claims = decode_access_token(token, settings)
    except InvalidTokenError as exc:
        raise CREDENTIALS_EXCEPTION from exc

    user = get_user_by_id(session, claims.get("sub", ""))
    if user is None or not user.is_active:
        raise CREDENTIALS_EXCEPTION

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin(user: CurrentUser) -> User:
    """Anything that moves money or changes state."""
    if user.role is not UserRole.ADMIN:
        # 403, not 404: the caller is authenticated, they simply are not
        # allowed. Hiding that would only make the API confusing to use.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"this operation requires the {UserRole.ADMIN.value} role",
        )
    return user


AdminUser = Annotated[User, Depends(require_admin)]
