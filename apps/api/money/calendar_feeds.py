"""Renewals calendar feeds (reports): a per-person secret URL a calendar app subscribes to.

The secret is `swc_` plus 40 URL-safe characters, shown once (in the create and rotate responses); only its SHA-256
is stored (high-entropy, so a slow hash adds nothing; as api/tokens.py). It goes in the feed URL's query string, not
its path, because gunicorn's access log writes the path only (gunicorn.conf.py), so the secret never reaches a log
line; it is never in an audit payload or snapshot either (the audit keys are the owner and the prefix).

A feed reads as its owner in its organization: it stops working when revoked, when the owner is deactivated or leaves
the organization. Every refusal is the same 404 (`authenticate` returns None), so a guess learns nothing about which
feeds exist. One live feed per person per organization; rotating revokes the old row and makes a new one, so a revoked
secret never works again. Create, rotate and revoke are audited (calendar_feed.created / .rotated / .revoked)."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from catalog.services import audit
from core.models import Membership
from core.problems import Conflict

from .models import CalendarFeed

SECRET_PREFIX = "swc_"  # noqa: S105  (a format marker, not a password)
SECRET_CHARS = 40
PREFIX_LENGTH = 12  # "swc_" plus 8 characters: tells feeds apart, far too little to guess the rest
LAST_USED_RESOLUTION = timedelta(minutes=1)
ALREADY = "You already have a calendar feed. Rotate it to get a new address, or revoke it first."


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def generate_secret() -> str:
    return SECRET_PREFIX + secrets.token_urlsafe(SECRET_CHARS)[:SECRET_CHARS]


def live_feed(org, user) -> CalendarFeed | None:
    return CalendarFeed.objects.filter(organization=org, user=user, revoked_at__isnull=True).first()


def _values(feed: CalendarFeed) -> dict:
    # Keys avoid the audit denylist word "token", which would redact them; the prefix is what the UI shows.
    return {"owner": feed.user_id, "prefix": feed.prefix}


def _new(org, user) -> tuple[CalendarFeed, str]:
    secret = generate_secret()
    try:
        with transaction.atomic():
            feed = CalendarFeed.objects.create(
                organization=org, user=user, token_hash=hash_secret(secret), prefix=secret[:PREFIX_LENGTH]
            )
    except IntegrityError:  # another request made one at the same moment (the one-live-feed constraint)
        raise Conflict(ALREADY) from None
    return feed, secret


@transaction.atomic
def create_feed(org, user) -> tuple[CalendarFeed, str]:
    """(feed, secret). The secret exists only in this return value. Refused (409) while the person has a live feed."""
    if live_feed(org, user) is not None:
        raise Conflict(ALREADY)
    feed, secret = _new(org, user)
    audit(org, user, "calendar_feed.created", feed, _values(feed))
    return feed, secret


@transaction.atomic
def rotate_feed(org, user, feed: CalendarFeed) -> tuple[CalendarFeed, str]:
    """Revoke `feed` and make a new one in one transaction: the old address stops working at once."""
    locked = CalendarFeed.objects.select_for_update().get(id=feed.id)
    if locked.revoked_at is not None:
        raise Conflict("This calendar feed was revoked meanwhile. Create a new one.")
    locked.revoked_at = timezone.now()
    locked.save(update_fields=["revoked_at", "updated_at"])
    new, secret = _new(org, user)
    audit(org, user, "calendar_feed.rotated", new, {**_values(new), "replaces": str(locked.id)})
    return new, secret


@transaction.atomic
def revoke_feed(org, actor, feed: CalendarFeed) -> CalendarFeed:
    locked = CalendarFeed.objects.select_for_update().get(id=feed.id)
    if locked.revoked_at is None:
        locked.revoked_at = timezone.now()
        locked.save(update_fields=["revoked_at", "updated_at"])
        audit(org, actor, "calendar_feed.revoked", locked, _values(locked))
    return locked


def authenticate(secret: object) -> tuple[CalendarFeed, Membership] | None:
    """The live feed for this secret and its owner's membership in the feed's organization, or None for anything that
    isn't one (unknown, malformed, revoked, owner inactive or no longer a member). Notes the use at most once a
    minute."""
    if not isinstance(secret, str) or not secret.startswith(SECRET_PREFIX) or len(secret) > 100:
        return None
    feed = (
        CalendarFeed.objects.select_related("organization", "user")
        .filter(token_hash=hash_secret(secret), revoked_at__isnull=True)
        .first()
    )
    if feed is None or not feed.user.is_active:
        return None
    membership = (
        Membership.objects.select_related("organization", "user")
        .filter(user=feed.user, organization_id=feed.organization_id)
        .first()
    )
    if membership is None:
        return None
    now = timezone.now()
    if feed.last_used_at is None or now - feed.last_used_at >= LAST_USED_RESOLUTION:
        CalendarFeed.objects.filter(pk=feed.pk).update(last_used_at=now)
        feed.last_used_at = now
    return feed, membership
