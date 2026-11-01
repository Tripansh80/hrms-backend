"""Authentication request and response models."""

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class PasswordRequest(BaseModel):
    email: EmailStr


class AcceptInviteRequest(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    password: str = Field(min_length=12, max_length=256)


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    password: str = Field(min_length=12, max_length=256)


class SignupRequest(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    slug: str = Field(min_length=2, max_length=63, pattern=r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
    owner_email: EmailStr
    owner_full_name: str = Field(min_length=2, max_length=160)
    password: str = Field(min_length=12, max_length=256)
