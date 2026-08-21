"""
One-time helper: obtain a Gmail refresh token for the digest sender.

Run this ON YOUR OWN MACHINE, not in the container. It opens a browser, you
consent as the sending mailbox, and it prints a refresh token which you then put
into Secret Manager.

    export GMAIL_CLIENT_ID='...apps.googleusercontent.com'
    export GMAIL_CLIENT_SECRET='...'
    pip3 install --user google-auth-oauthlib
    python3 get_gmail_refresh_token.py

Why this exists rather than domain-wide delegation: a refresh token can send as
exactly ONE mailbox. Adding `gmail.send` to the service account's domain-wide
delegation would let it send as any user in the domain -- far more than the
digest needs, and a grant that outlives whatever it was added for.

THE OUTPUT IS A CREDENTIAL.
  * Do not paste it into chat, a ticket, or a commit.
  * Do not save it to a file in this repository.
  * Put it straight into Secret Manager and close the terminal.
  * It can be revoked at any time from the sending account's
    Security -> Your connections to third-party apps.
"""
import os
import sys

SCOPES = ['https://www.googleapis.com/auth/gmail.send']


def main() -> int:
    client_id = os.environ.get('GMAIL_CLIENT_ID')
    client_secret = os.environ.get('GMAIL_CLIENT_SECRET')
    if not client_id or not client_secret:
        print("Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET first "
              "(from the Desktop-app OAuth client).", file=sys.stderr)
        return 2

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("pip3 install --user google-auth-oauthlib", file=sys.stderr)
        return 2

    flow = InstalledAppFlow.from_client_config(
        {"installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }},
        scopes=SCOPES)

    # access_type=offline is what asks for a refresh token at all, and
    # prompt=consent forces one to be RE-ISSUED: Google returns a refresh token
    # only on first consent, so without this a second run gives an access token
    # and nothing else, which looks like the flow silently not working.
    creds = flow.run_local_server(port=0, access_type='offline',
                                  prompt='consent',
                                  authorization_prompt_message=(
                                      "Sign in as the SENDING mailbox "
                                      "(e.g. admin@enopieastcobb.com), not your "
                                      "personal account. Opening browser...\n"))

    if not creds.refresh_token:
        print("No refresh token came back. Re-run -- prompt=consent should "
              "force one.", file=sys.stderr)
        return 1

    print()
    print("Granted to:", ", ".join(creds.scopes or SCOPES))
    print()
    print("REFRESH TOKEN (treat as a password, put straight into Secret Manager):")
    print()
    print(creds.refresh_token)
    print()
    print("Next:")
    print("  gcloud secrets versions add digest-gmail-refresh-token \\")
    print("      --data-file=- --project enopieastcobb-studentprogress")
    print("  ...then paste it, press Ctrl-D, and clear your scrollback.")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
