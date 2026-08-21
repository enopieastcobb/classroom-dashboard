"""
Sends the outstanding-booklets digest by email, through the Gmail API.

Gmail rather than SendGrid or Mailgun because the domain-wide delegation used
for Classroom and Sheets already works: this is one more scope on the same
mechanism, with no account to open, no API key to store, and no third party
holding children's names.

The scope is `gmail.send`, which cannot read, search or delete anything. Mail is
sent AS the sender address, so it lands in that mailbox's Sent folder.
"""
import base64
import logging
from email.message import EmailMessage
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

GMAIL_SCOPES = ['https://www.googleapis.com/auth/gmail.send']


def _plain(subject_line: str, sections: List[Dict[str, Any]], date_label: str,
           checked: int) -> str:
    """
    The digest as text.

    Plain text on purpose: it is read on a phone, forwarded, and occasionally
    printed, and a name in a table cell is no easier to act on than a name on
    its own line. Ordered by hour so it reads the way the day ran.
    """
    lines = [f"Booklets still outstanding — {date_label}", ""]
    if not sections:
        lines += ["Nothing outstanding. Every child scheduled today was handed "
                  "the booklets they were due.", ""]
    for sec in sections:
        lines.append(f"{sec['time']} · {sec['room']}")
        for item in sec['items']:
            flag = "** " if item['severe'] else "   "
            lines.append(f"{flag}{item['student']} — {item['detail']}")
        lines.append("")
    lines += [
        "-" * 58,
        f"{checked} sessions checked. Lines marked ** are mistakes to correct "
        "rather than booklets simply not handed over yet: a level test never "
        "started, or the same booklet issued twice.",
        "",
        "This is read from Google Classroom, so it clears itself once the "
        "booklets are assigned. Sent by the classroom dashboard at "
        "classroom.enopieastcobb.com.",
    ]
    return "\n".join(lines)


def send_digest(creds, *, sender: str, to: str, subject: str,
                sections: List[Dict[str, Any]], date_label: str,
                checked: int) -> str:
    """
    Send the digest. Returns the Gmail message id.

    `creds` must be delegated to `sender` and carry GMAIL_SCOPES -- Gmail will
    not send on behalf of an address the token was not minted for.
    """
    from googleapiclient.discovery import build

    msg = EmailMessage()
    msg['To'] = to
    msg['From'] = sender
    msg['Subject'] = subject
    msg.set_content(_plain(subject, sections, date_label, checked))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service = build('gmail', 'v1', credentials=creds, cache_discovery=False)
    sent = service.users().messages().send(userId='me', body={'raw': raw}).execute()
    logger.info("Digest sent to %s as %s (message %s)", to, sender, sent.get('id'))
    return sent.get('id') or ''
