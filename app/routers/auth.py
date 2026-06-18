import os
from datetime import datetime, timezone
from pathlib import Path

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.config import Config

from app.database import get_db
from app.models import User

router = APIRouter(tags=["auth"])
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")

config = Config()
oauth = OAuth(config)
oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
    authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    access_token_url="https://oauth2.googleapis.com/token",
    jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
    userinfo_endpoint="https://openidconnect.googleapis.com/v1/userinfo",
    client_kwargs={"scope": "openid email profile"},
)


@router.get("/login")
def login_page(request: Request):
    """Render the sign-in page. Redirects to /dashboard if already logged in."""
    if request.session.get("user_id"):
        return RedirectResponse(url="/dashboard", status_code=302)
    return templates.TemplateResponse(request, "login.html", {})


@router.get("/auth/google")
async def auth_google(request: Request):
    """Redirect the browser to Google's OAuth consent screen."""
    redirect_uri = request.url_for("auth_callback")
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request, db: Session = Depends(get_db)):
    """Handle the OAuth callback from Google; create/update the user; start session."""
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception:
        return RedirectResponse(url="/login?error=oauth_failed", status_code=302)

    user_info = token.get("userinfo") or await oauth.google.userinfo(token=token)
    google_id = user_info.get("sub")
    email = user_info.get("email", "")
    display_name = user_info.get("name", "")

    if not google_id:
        return RedirectResponse(url="/login?error=no_id", status_code=302)

    # Get or create user
    user = db.query(User).filter(User.google_id == google_id).first()
    if user:
        user.last_login_at = datetime.now(timezone.utc)
        if display_name and not user.display_name:
            user.display_name = display_name
    else:
        user = User(
            google_id=google_id,
            email=email,
            display_name=display_name,
        )
        db.add(user)
    db.commit()
    db.refresh(user)

    request.session["user_id"] = user.id
    return RedirectResponse(url="/dashboard", status_code=302)


@router.get("/logout")
def logout(request: Request):
    """Clear the session and redirect to the login page."""
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)
