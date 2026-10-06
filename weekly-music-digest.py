"""
weekly-music-digest Lambda (Last.fm version)

Trigger: an EventBridge Scheduled Rule, once a week (e.g. every Sunday
morning). No form, no API Gateway — this runs entirely on its own.

What it does:
  1. For each tag (genre) in TARGET_GENRES, pulls that tag's top albums
     from Last.fm (tag.getTopAlbums). Only an API key is needed — no
     user login, no premium account.
  2. Checks DynamoDB to skip anything already sent in a previous week,
     paging deeper into each tag's list until it finds unsent albums.
  3. Interleaves genres so one genre doesn't dominate the email.
  4. Emails the list to one or more recipients via SES.
  5. Records what was sent, so next week doesn't repeat it.

NOTE: Last.fm has no "new releases" endpoint, so these are popular
albums per tag, not necessarily brand-new ones. Because sent albums are
skipped, each week surfaces the next batch down each tag's chart.

Environment variables:
  LASTFM_API_KEY      from https://www.last.fm/api/account/create
  TARGET_GENRES       comma-separated Last.fm tags, e.g. "r&b,jazz,alternative hip-hop"
  DYNAMODB_TABLE      e.g. "music-digest-sent" (partition key: album_id, String)
  SENDER_EMAIL        your SES-verified sending address
  RECIPIENT_EMAIL     one or more addresses, comma-separated, e.g.
                      "you@example.com,friend@example.com"
                      (if SES is in sandbox mode, every recipient must be
                      a verified identity)
  MAX_RESULTS         optional, default 10 — cap on albums per email
  PAGES_PER_GENRE     optional, default 3 — max pages (50 albums each) to
                      scan per tag looking for unsent albums
  SEED_ARTISTS        optional, comma-separated artists you already like,
                      e.g. "SZA,Tyler The Creator,Frank Ocean"
  SIMILAR_PER_SEED    optional, default 5 — similar artists pulled per seed
  ALBUMS_PER_SIMILAR_ARTIST  optional, default 3 — top albums pulled per
                      similar artist

IAM role needs:
  - AWSLambdaBasicExecutionRole
  - dynamodb:GetItem and dynamodb:PutItem on the DYNAMODB_TABLE
  - ses:SendEmail

Lambda timeout: set to ~30-60s (several sequential Last.fm calls).
"""

import json
import os
import logging
import boto3
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from itertools import zip_longest
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
ses = boto3.client("ses")

API_KEY = os.environ.get("LASTFM_API_KEY")
TARGET_GENRES = [g.strip().lower() for g in os.environ.get("TARGET_GENRES", "").split(",") if g.strip()]
TABLE_NAME = os.environ.get("DYNAMODB_TABLE", "music-digest-sent")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "")
# Supports a single address or a comma-separated list
RECIPIENT_EMAILS = [e.strip() for e in os.environ.get("RECIPIENT_EMAIL", "").split(",") if e.strip()]
MAX_RESULTS = int(os.environ.get("MAX_RESULTS", "10"))
PAGES_PER_GENRE = int(os.environ.get("PAGES_PER_GENRE", "3"))
PAGE_SIZE = 50
SEED_ARTISTS = [a.strip() for a in os.environ.get("SEED_ARTISTS", "").split(",") if a.strip()]
SIMILAR_PER_SEED = int(os.environ.get("SIMILAR_PER_SEED", "5") or "5")
ALBUMS_PER_SIMILAR_ARTIST = int(os.environ.get("ALBUMS_PER_SIMILAR_ARTIST", "3") or "3")

table = dynamodb.Table(TABLE_NAME)


def lastfm_get(method, params=None):
    """Call the Last.fm API. Only needs an API key (no auth flow)."""
    query = {"method": method, "api_key": API_KEY, "format": "json"}
    if params:
        query.update(params)
    url = "https://ws.audioscrobbler.com/2.0/?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url, headers={"User-Agent": "weekly-music-digest/1.0"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode())
    # Last.fm reports errors in the JSON body
    if "error" in data:
        raise RuntimeError(f"Last.fm error {data['error']}: {data.get('message')}")
    return data


def get_top_albums_for_tag(tag, page):
    data = lastfm_get("tag.gettopalbums", {"tag": tag, "limit": PAGE_SIZE, "page": page})
    albums = data.get("albums", {}).get("album", [])
    # Last.fm's JSON can return a single dict instead of a list
    if isinstance(albums, dict):
        albums = [albums]
    return albums


def get_similar_artists(artist_name, limit=5):
    data = lastfm_get("artist.getsimilar", {"artist": artist_name, "limit": limit})
    similar = data.get("similarartists", {}).get("artist", [])
    if isinstance(similar, dict):
        similar = [similar]
    return [a.get("name") for a in similar if a.get("name")]


def get_top_albums_for_artist(artist_name, limit=3):
    data = lastfm_get("artist.gettopalbums", {"artist": artist_name, "limit": limit})
    albums = data.get("topalbums", {}).get("album", [])
    if isinstance(albums, dict):
        albums = [albums]
    return albums


def album_key(artist, album):
    """Stable ID for dedupe. Last.fm mbids are often missing, so use names."""
    return f"{artist.strip().lower()}::{album.strip().lower()}"


def already_sent(album_id):
    try:
        item = table.get_item(Key={"album_id": album_id}).get("Item")
        return item is not None
    except ClientError as e:
        logger.error(f"DynamoDB read failed: {str(e)}")
        return False


def mark_as_sent(album_id, album_name, artist_name):
    try:
        table.put_item(Item={
            "album_id": album_id,
            "album_name": album_name,
            "artist_name": artist_name,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        })
    except ClientError as e:
        logger.error(f"DynamoDB write failed: {str(e)}")


def collect_unsent_for_genre(tag, seen):
    """Page through a tag's top albums until we have enough unsent ones."""
    found = []
    for page in range(1, PAGES_PER_GENRE + 1):
        try:
            albums = get_top_albums_for_tag(tag, page)
        except Exception as e:
            logger.error(f"Last.fm lookup failed for tag '{tag}' page {page}: {str(e)}")
            break
        if not albums:
            break

        for album in albums:
            artist_name = (album.get("artist") or {}).get("name")
            album_name = album.get("name")
            if not artist_name or not album_name:
                continue

            key = album_key(artist_name, album_name)
            if key in seen:
                continue
            seen.add(key)

            if already_sent(key):
                continue

            found.append({
                "id": key,
                "name": album_name,
                "artist": artist_name,
                "genres": [tag],
                "url": album.get("url", ""),
            })
            if len(found) >= MAX_RESULTS:
                return found
    return found


def collect_unsent_for_similar_artist(seed_artist, seen):
    """Pulls albums from artists similar to one of your seed artists."""
    found = []
    try:
        similar_artists = get_similar_artists(seed_artist, SIMILAR_PER_SEED)
    except Exception as e:
        logger.error(f"Last.fm similar-artist lookup failed for '{seed_artist}': {str(e)}")
        return found

    for similar_artist in similar_artists:
        try:
            albums = get_top_albums_for_artist(similar_artist, ALBUMS_PER_SIMILAR_ARTIST)
        except Exception as e:
            logger.error(f"Last.fm top-albums lookup failed for '{similar_artist}': {str(e)}")
            continue

        for album in albums:
            album_name = album.get("name")
            if not album_name:
                continue
            key = album_key(similar_artist, album_name)
            if key in seen:
                continue
            seen.add(key)
            if already_sent(key):
                continue
            found.append({
                "id": key,
                "name": album_name,
                "artist": similar_artist,
                "genres": [f"similar to {seed_artist}"],
                "url": album.get("url", ""),
            })
            if len(found) >= MAX_RESULTS:
                return found
    return found


def build_email_body(releases):
    lines = ["Here's this week's picks, filtered to your genres:\n"]
    for r in releases:
        lines.append(f"🎵 {r['name']} — {r['artist']}")
        lines.append(f"   Genre: {', '.join(r['genres'])}")
        lines.append(f"   Listen: {r['url']}\n")
    return "\n".join(lines)


def lambda_handler(event, context):
    logger.info("Starting weekly music digest run")

    if not API_KEY:
        logger.error("LASTFM_API_KEY not set")
        return {"status": "error", "message": "LASTFM_API_KEY not configured"}

    if not RECIPIENT_EMAILS:
        logger.error("RECIPIENT_EMAIL not set — nobody to send the digest to")
        return {"status": "error", "message": "RECIPIENT_EMAIL not configured"}

    if not TARGET_GENRES and not SEED_ARTISTS:
        logger.error("Neither TARGET_GENRES nor SEED_ARTISTS is set — nothing to pull")
        return {"status": "error", "message": "No discovery source configured"}

    seen = set()  # avoids duplicates when an album shows up under multiple tags/sources
    per_genre = [collect_unsent_for_genre(tag, seen) for tag in TARGET_GENRES]
    similar_groups = [collect_unsent_for_similar_artist(artist, seen) for artist in SEED_ARTISTS]

    # Round-robin across genres AND similar-artist sources so the email
    # is a mix, then cap at MAX_RESULTS
    matches = []
    for group in zip_longest(*(per_genre + similar_groups)):
        for item in group:
            if item is not None:
                matches.append(item)
    matches = matches[:MAX_RESULTS]

    if not matches:
        logger.info("No new matching releases this week")
        return {"status": "no_matches"}

    body = build_email_body(matches)

    try:
        ses.send_email(
            Source=SENDER_EMAIL,
            Destination={"BccAddresses": RECIPIENT_EMAILS},
            Message={
                "Subject": {"Data": f"🎧 Fresh Rotation — {len(matches)} picks for the week"},
                "Body": {"Text": {"Data": body}},
            },
        )
    except ClientError as e:
        logger.error(f"SES send failed: {str(e)}")
        return {"status": "error", "message": "Failed to send email"}

    for r in matches:
        mark_as_sent(r["id"], r["name"], r["artist"])

    logger.info(f"Sent digest with {len(matches)} releases to {len(RECIPIENT_EMAILS)} recipient(s)")
    return {"status": "sent", "count": len(matches), "recipients": len(RECIPIENT_EMAILS)}