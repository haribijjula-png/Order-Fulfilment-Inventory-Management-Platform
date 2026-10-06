from fastapi import APIRouter, Depends
from sqlalchemy import select
from app.db import get_db
from app.deps import current_user
from app.models import CUSTOMER
from app.schemas import ChangePasswordIn, LoginIn, RefreshIn, RegisterIn, TokenOut, UserOut
from app.services import auth as svc

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=201)
def register(data: RegisterIn, db=Depends(get_db)):
    u = svc.create_user(db, data, CUSTOMER)  # public signup is always a customer
    db.commit()
    return u


@router.post("/login", response_model=TokenOut)
def login(data: LoginIn, db=Depends(get_db)):
    return svc.login(db, data)


@router.post("/refresh", response_model=TokenOut)
def refresh(data: RefreshIn, db=Depends(get_db)):
    return svc.refresh(db, data.refresh_token)


@router.post("/logout", status_code=204)
def logout(data: RefreshIn, db=Depends(get_db)):
    svc.logout(db, data.refresh_token)


@router.get("/me", response_model=UserOut)
def me(user=Depends(current_user)):
    return user


@router.post("/change-password", status_code=204)
def change_password(data: ChangePasswordIn, user=Depends(current_user), db=Depends(get_db)):
    svc.change_password(db, user, data.old_password, data.new_password)
