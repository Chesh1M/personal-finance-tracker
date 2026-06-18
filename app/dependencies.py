from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User


class RequiresLoginException(Exception):
    """Raised by get_current_user when no valid session exists.
    Caught by the app-level handler in main.py, which redirects to /login."""
    pass


async def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise RequiresLoginException()
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise RequiresLoginException()
    return user
