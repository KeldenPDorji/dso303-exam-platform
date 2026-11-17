"""Turns the Cognito claims that API Gateway already verified into a Principal.

API Gateway's JWT authorizer checks the token signature, expiry, issuer and
audience before Lambda runs, so this module only reads the verified claims and
enforces role-based access (Cognito groups Students / Tutors / Admins).
"""
from dataclasses import dataclass

from .http import HttpError

ROLES = ("Admins", "Tutors", "Students")  # highest privilege first


def parse_groups(raw):
    """HTTP API flattens the cognito:groups array to a string such as "[Tutors Admins]"."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(g) for g in raw]
    text = str(raw).strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return [g.strip().strip('"') for g in text.replace(",", " ").split() if g.strip().strip('"')]


@dataclass(frozen=True)
class Principal:
    sub: str
    email: str
    groups: tuple

    @property
    def role(self):
        for role in ROLES:
            if role in self.groups:
                return role
        return None

    def has(self, *roles):
        return any(r in self.groups for r in roles)

    def require(self, *roles):
        if not self.has(*roles):
            raise HttpError(403, "This action requires the role: " + " or ".join(roles))


def principal_from_event(event):
    claims = (
        ((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt", {}).get("claims")
        or {}
    )
    if not claims.get("sub"):
        raise HttpError(401, "Not signed in")
    if claims.get("token_use") not in (None, "id"):
        raise HttpError(401, "Send the Cognito ID token")
    groups = tuple(g for g in parse_groups(claims.get("cognito:groups")) if g in ROLES)
    if not groups:
        raise HttpError(403, "Your account has no role yet. Ask an administrator to add you to a group.")
    return Principal(sub=claims["sub"], email=claims.get("email", ""), groups=groups)
