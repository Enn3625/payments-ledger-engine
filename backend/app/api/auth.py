"""POST /auth/login and GET /auth/me.

Login takes an OAuth2 password form rather than JSON so the Swagger UI
"Authorize" button works out of the box -- which matters for a demo someone
else is going to click through.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.config import Settings, get_settings
from app.db import get_session
from app.models import User, UserRole
from app.services.auth import AuthError, authenticate_user, create_access_token

router = APIRouter(prefix="/auth", tags=["auth"])


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: UserRole


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    role: UserRole
    is_active: bool
    created_at: datetime


@router.post("/login", response_model=TokenResponse)
def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TokenResponse:
    """Exchange email and password for a bearer token.

    `username` is the email -- the field name comes from the OAuth2 spec.
    """
    try:
        user = authenticate_user(session, email=form.username, password=form.password)
    except AuthError as exc:
        # One message for wrong email, wrong password and deactivated account,
        # so this endpoint cannot be used to enumerate who has an account.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    token, expires_in = create_access_token(user, settings)
    return TokenResponse(access_token=token, expires_in=expires_in, role=user.role)


@router.get("/me", response_model=UserRead)
def read_current_user(user: CurrentUser) -> User:
    """Who the bearer token belongs to, straight from the database."""
    return user
