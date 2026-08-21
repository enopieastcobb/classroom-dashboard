"""
The outstanding-booklets digest, as a batch job rather than an HTTP endpoint.

Run to completion by Cloud Run Jobs on a schedule, so the digest has NO network
surface of its own:

  * no route, so nothing to reach even with a valid token
  * no shared secret to hold, rotate, or leak into a terminal
  * the service's ingress stays `internal-and-cloud-load-balancing`, which keeps
    IAP the single front door for everything a person can reach

An HTTP endpoint would have needed ingress opened so Cloud Scheduler could
call it, which puts a second door in a design whose whole point is one.
Guarding that door with a token is weaker than not having it.

Same image as the service, same service account, so Secret Manager, delegated
Classroom access and Firestore work identically. Only the command differs.

    python3 digest_job.py [YYYY-MM-DD]

With no argument it uses today at the centre. Exits non-zero on failure so a
failed execution is visible in the Cloud Run Jobs history rather than silent.
"""
import logging
import sys
from datetime import date

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("digest_job")


def main(argv) -> int:
    # Imported here, not at module scope, so a configuration problem is reported
    # by this script's own error handling rather than as an import traceback.
    import main as app
    import attendance_service as attendance
    import email_service

    if len(argv) > 1:
        try:
            on = date.fromisoformat(argv[1])
        except ValueError:
            logger.error("Not a date: %r. Use YYYY-MM-DD.", argv[1])
            return 2
    else:
        on = app.today_local()

    force = '--force' in argv

    # One email per date, however often the job runs. Cloud Scheduler retries a
    # failed execution, and a job can be started by hand, so without this a bad
    # afternoon could empty the mailbox's daily sending quota.
    if not force and attendance.digest_already_sent(on.isoformat()):
        logger.info("Digest for %s already sent; nothing to do.", on)
        return 0

    sections, checked = app._digest_sections(on, app.DIGEST_SENDER)
    outstanding = sum(len(s["items"]) for s in sections)
    label = on.strftime('%a %b %-d')
    subject = (f"Booklets outstanding — {label}" if outstanding
               else f"Nothing outstanding — {label}")

    creds = email_service.sender_credentials()
    msg_id = email_service.send_digest(
        creds, sender=app.DIGEST_SENDER, to=app.DIGEST_TO, subject=subject,
        sections=sections, date_label=label, checked=checked)
    attendance.record_digest_sent(on.isoformat(), to=app.DIGEST_TO,
                                  findings=outstanding, message_id=msg_id)

    logger.info("DIGEST SENT for %s: %d findings across %d sessions (message %s)",
                on, outstanding, checked, msg_id)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main(sys.argv))
    except SystemExit:
        raise
    except Exception as exc:
        # Non-zero so the execution shows as failed in the Jobs history. A
        # digest that silently stops being sent is worse than one that fails
        # loudly, because nobody notices the absence of an email.
        logger.error("Digest failed: %s", exc, exc_info=True)
        raise SystemExit(1)
