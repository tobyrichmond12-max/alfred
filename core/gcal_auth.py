"""Google OAuth setup and scoped credentials accessor.

Exports:
    get_credentials(scopes=None)  -- returns refreshed google.oauth2.Credentials
                                     for the requested scope list. If scopes
                                     include anything not in the stored token,
                                     raises RuntimeError with a clear message
                                     so the caller can guide the user to run
                                     the consent flow.

Running this file as a script runs the one-time consent flow. Pass a
space-separated list of scopes on argv to consent to more than the
default calendar scope, or use --all to consent to calendar + gmail.
"""
import json
import os
import sys
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(__file__))
from gcal import CREDS_FILE, TOKEN_FILE

DEFAULT_SCOPES = ["https://www.googleapis.com/auth/calendar"]
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
ALL_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    GMAIL_SCOPE,
]


def _token_scopes():
    if not os.path.exists(TOKEN_FILE):
        return []
    try:
        with open(TOKEN_FILE) as f:
            data = json.load(f)
        return data.get("scopes") or []
    except Exception:
        return []


def get_credentials(scopes=None):
    """Return refreshed credentials covering every requested scope.

    If the stored token does not already cover the requested scopes,
    raise RuntimeError pointing the caller at the consent flow.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    requested = list(scopes) if scopes else list(DEFAULT_SCOPES)
    stored = _token_scopes()

    if not os.path.exists(TOKEN_FILE):
        raise RuntimeError(
            "No token. Run: python3 /mnt/nvme/alfred/core/gcal_auth.py"
        )

    missing = [s for s in requested if s not in stored]
    if missing:
        raise RuntimeError(
            "Token is missing scopes "
            + ",".join(missing)
            + ". Re-run: python3 /mnt/nvme/alfred/core/gcal_auth.py --all"
        )

    creds = Credentials.from_authorized_user_file(TOKEN_FILE, requested)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(TOKEN_FILE, "w") as f:
                f.write(creds.to_json())
        else:
            raise RuntimeError(
                "Token invalid and unrefreshable. Re-run gcal_auth.py."
            )

    return creds


def run_consent(scopes):
    if not os.path.exists(CREDS_FILE):
        print(f"\nError: credentials not found at:\n  {CREDS_FILE}")
        sys.exit(1)

    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_secrets_file(
        CREDS_FILE,
        scopes=scopes,
        redirect_uri="http://localhost",
    )
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")

    print("\n" + "=" * 60)
    print("STEP 1: Open this URL in any browser (phone or laptop):")
    print("=" * 60)
    print(f"\n{auth_url}\n")
    print("=" * 60)
    print("STEP 2: Authorize Alfred with the requested scopes.")
    print("STEP 3: Your browser will fail to load 'localhost'. That is fine.")
    print("STEP 4: Copy the FULL URL from your browser's address bar.")
    print("        It will look like: http://localhost/?code=4/0A...&scope=...")
    print("=" * 60)

    redirect_url = input("\nPaste the full redirect URL here: ").strip()

    try:
        parsed = urlparse(redirect_url)
        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        if not code:
            code = redirect_url.strip()
    except Exception:
        code = redirect_url.strip()

    if not code:
        print("Error: could not extract authorization code.")
        sys.exit(1)

    flow.fetch_token(code=code)
    creds = flow.credentials

    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())

    print(f"\nSuccess. Token saved to:\n  {TOKEN_FILE}")
    print("Scopes now covered:", ", ".join(creds.scopes or []))


def main():
    argv = sys.argv[1:]
    if "--all" in argv:
        run_consent(ALL_SCOPES)
        return
    if argv:
        run_consent(argv)
        return
    run_consent(DEFAULT_SCOPES)


if __name__ == "__main__":
    main()
