import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Any
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from meridian.config import settings
from meridian.db import get_db
from meridian.errors import DomainError
from meridian.governance import record
from meridian.models import LoginState, Principal, RoleAssignment, WebSession, now
from meridian.people_models import AuthzPolicy
from meridian.policy import POLICY_DEFINITION, POLICY_ID, canonical_hash
from meridian.schemas import Grant, Me

router = APIRouter()
DB = Annotated[Session, Depends(get_db)]


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass
class Actor:
    principal: Principal
    grants: list[RoleAssignment]
    session: WebSession
    policy: dict[str, Any]

    @property
    def capabilities(self) -> set[str]:
        return set().union(*(set(self.policy.get(grant.role, [])) for grant in self.grants))


def scopes(current: Actor, capability: str) -> set[str]:
    return {
        grant.scope for grant in current.grants if capability in current.policy.get(grant.role, [])
    }


def actor(request: Request, db: DB) -> Actor:
    token = request.cookies.get("meridian_session", "")
    session = db.get(WebSession, digest(token)) if token else None
    if session is None or session.revoked or session.expires_at <= now():
        raise DomainError(401, "unauthenticated", "Sign in with a valid account.")
    principal = db.get(Principal, session.principal_id)
    if principal is None or not principal.active:
        raise DomainError(401, "unauthenticated", "Account is unavailable.")
    grants = list(
        db.scalars(
            select(RoleAssignment).where(
                RoleAssignment.principal_id == principal.id,
                RoleAssignment.effective_from <= now(),
                or_(RoleAssignment.effective_to.is_(None), RoleAssignment.effective_to > now()),
            )
        )
    )
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if request.headers.get("origin") != settings().web_origin or not secrets.compare_digest(
            request.headers.get("x-csrf-token", ""), session.csrf_token
        ):
            record(
                db,
                principal.id,
                "security.csrf_denied",
                "session",
                principal.id,
                request.state.request_id,
                False,
            )
            db.commit()
            raise DomainError(403, "csrf_denied", "Request origin or CSRF token is invalid.")
    policy = db.get(AuthzPolicy, POLICY_ID)
    if (
        policy is None
        or policy.effective_at > now()
        or policy.sha256 != canonical_hash(policy.definition)
        or policy.sha256 != canonical_hash(POLICY_DEFINITION)
    ):
        raise DomainError(
            503, "policy_unavailable", "The active authorization policy is unavailable."
        )
    return Actor(principal, grants, session, policy.definition)


CurrentActor = Annotated[Actor, Depends(actor)]


def require(actor: Actor, capability: str, db: Session, request: Request) -> None:
    if capability not in actor.capabilities:
        record(
            db,
            actor.principal.id,
            "security.access_denied",
            "route",
            request.url.path,
            request.state.request_id,
            False,
        )
        db.commit()
        raise DomainError(403, "forbidden", "Your current grants do not permit this action.")


def validate_id_token(token: str, jwks: dict[str, Any], nonce: str) -> dict[str, Any]:
    header = jwt.get_unverified_header(token)
    if header.get("alg") != "RS256":
        raise jwt.InvalidAlgorithmError("Unsupported algorithm")
    keys = [key for key in jwks["keys"] if key.get("kid") == header.get("kid")]
    if len(keys) != 1:
        raise jwt.InvalidKeyError("Unknown signing key")
    key = jwt.PyJWK.from_dict(keys[0], algorithm="RS256")
    claims: dict[str, Any] = jwt.decode(
        token,
        key.key,
        algorithms=["RS256"],
        audience=settings().oidc_client_id,
        issuer=settings().oidc_issuer,
        options={"require": ["exp", "iat", "iss", "sub", "aud", "nonce"]},
    )
    if not isinstance(claims.get("nonce"), str) or not secrets.compare_digest(
        claims["nonce"], nonce
    ):
        raise jwt.InvalidTokenError("Nonce mismatch")
    if (
        isinstance(claims["aud"], list)
        and len(claims["aud"]) > 1
        and claims.get("azp") != settings().oidc_client_id
    ):
        raise jwt.InvalidTokenError("Authorized party mismatch")
    return claims


@router.get("/auth/login", include_in_schema=False)
def login(db: DB) -> RedirectResponse:
    state, browser, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(4))
    db.add(
        LoginState(
            state_hash=digest(state),
            nonce=nonce,
            verifier=verifier,
            browser_hash=digest(browser),
            expires_at=now() + timedelta(minutes=5),
        )
    )
    db.commit()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    query = urlencode(
        {
            "client_id": settings().oidc_client_id,
            "redirect_uri": settings().oidc_redirect_uri,
            "response_type": "code",
            "scope": "openid",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "login",
        }
    )
    response = RedirectResponse(f"{settings().oidc_issuer}/auth?{query}")
    response.set_cookie(
        "meridian_login",
        browser,
        httponly=True,
        secure=settings().cookie_secure,
        samesite="lax",
        max_age=300,
        path="/auth",
    )
    return response


@router.get("/auth/callback", include_in_schema=False)
def callback(request: Request, db: DB, code: str = "", state: str = "") -> RedirectResponse:
    login_state = db.scalar(
        select(LoginState).where(LoginState.state_hash == digest(state)).with_for_update()
    )
    if (
        login_state is None
        or login_state.expires_at <= now()
        or not secrets.compare_digest(
            login_state.browser_hash, digest(request.cookies.get("meridian_login", ""))
        )
    ):
        raise DomainError(
            400, "invalid_login", "Login expired or browser binding did not match. Start again."
        )
    nonce, verifier = login_state.nonce, login_state.verifier
    db.delete(login_state)
    db.commit()
    try:
        with httpx.Client(timeout=8) as client:
            response = client.post(
                f"{settings().oidc_internal_origin or settings().oidc_issuer}/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": settings().oidc_redirect_uri,
                    "client_id": settings().oidc_client_id,
                    "code_verifier": verifier,
                },
            )
            response.raise_for_status()
            keys = client.get(f"{settings().oidc_internal_origin or settings().oidc_issuer}/jwks")
            keys.raise_for_status()
            claims = validate_id_token(response.json()["id_token"], keys.json(), nonce)
    except (httpx.HTTPError, jwt.PyJWTError, KeyError, ValueError) as exc:
        raise DomainError(
            401, "invalid_login", "The identity provider could not verify this login."
        ) from exc
    principal = db.scalar(
        select(Principal).where(Principal.oidc_sub == claims["sub"], Principal.active.is_(True))
    )
    if principal is None:
        raise DomainError(403, "unprovisioned", "This identity has no active Meridian account.")
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    db.add(
        WebSession(
            token_hash=digest(token),
            principal_id=principal.id,
            csrf_token=csrf,
            expires_at=now() + timedelta(hours=1),
        )
    )
    record(db, principal.id, "identity.login", "principal", principal.id, request.state.request_id)
    db.commit()
    redirect = RedirectResponse(settings().web_origin, status_code=303)
    redirect.set_cookie(
        "meridian_session",
        token,
        httponly=True,
        secure=settings().cookie_secure,
        samesite="lax",
        max_age=3600,
    )
    redirect.delete_cookie("meridian_login", path="/auth")
    return redirect


@router.get("/api/v1/me", response_model=Me)
def me(current: CurrentActor) -> Me:
    return Me(
        id=current.principal.id,
        display_name=current.principal.display_name,
        grants=[Grant(role=g.role, scope=g.scope) for g in current.grants],
        capabilities=sorted(current.capabilities),
        csrf_token=current.session.csrf_token,
        expires_at=current.session.expires_at,
        policy_version=POLICY_ID,
    )


@router.post("/api/v1/logout", status_code=204)
def logout(current: CurrentActor, db: DB, request: Request) -> Response:
    current.session.revoked = True
    record(
        db,
        current.principal.id,
        "identity.logout",
        "principal",
        current.principal.id,
        request.state.request_id,
    )
    db.commit()
    response = Response(status_code=204)
    response.delete_cookie("meridian_session")
    return response
