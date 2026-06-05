"""
Email service — sends interview invite emails to candidates.
Uses Python's built-in smtplib run in a thread (no extra dependency).
"""
import asyncio
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import settings

logger = logging.getLogger(__name__)


def _send_sync(to_email: str, subject: str, html_body: str) -> None:
    """Synchronous SMTP send — called via asyncio.to_thread so event loop is not blocked."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = settings.email_from
    msg["To"]      = to_email
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
        server.ehlo()
        server.starttls()
        server.login(settings.smtp_user, settings.smtp_password)
        server.sendmail(settings.email_from, to_email, msg.as_string())


async def send_interview_invite(
    candidate_name: str,
    candidate_email: str,
    job_title: str,
    join_url: str,
    expire_hours: int,
) -> None:
    """
    Send an interview invite email to the candidate.
    Safe to fire-and-forget — errors are logged, never raised.
    """
    if not settings.smtp_user or not settings.smtp_password:
        logger.warning(
            f"[email] SMTP not configured — skipping invite email to {candidate_email}. "
            f"Set SMTP_USER and SMTP_PASSWORD in .env to enable emails."
        )
        return

    subject   = f"Your Interview Invitation — {job_title}"
    html_body = _build_invite_html(candidate_name, job_title, join_url, expire_hours)

    try:
        await asyncio.to_thread(_send_sync, candidate_email, subject, html_body)
        logger.info(f"[email] Interview invite sent to {candidate_email}")
    except Exception as e:
        logger.error(
            f"[email] Failed to send invite to {candidate_email}: {e}",
            exc_info=True,
        )


def _build_invite_html(
    candidate_name: str,
    job_title: str,
    join_url: str,
    expire_hours: int,
) -> str:
    """Clean, professional HTML email template."""
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"/></head>
<body style="margin:0;padding:0;background:#f4f7ff;font-family:Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f7ff;padding:40px 0;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border-radius:12px;overflow:hidden;
                    box-shadow:0 2px 12px rgba(37,99,235,.10);">

        <!-- Header -->
        <tr><td style="background:#2563EB;padding:28px 36px;">
          <h1 style="margin:0;color:#ffffff;font-size:22px;font-weight:700;">
            AI HR Interview Invitation
          </h1>
          <p style="margin:6px 0 0;color:rgba(255,255,255,.75);font-size:14px;">
            {job_title}
          </p>
        </td></tr>

        <!-- Body -->
        <tr><td style="padding:32px 36px;">
          <p style="margin:0 0 16px;font-size:16px;color:#0f172a;">
            Dear <strong>{candidate_name}</strong>,
          </p>
          <p style="margin:0 0 20px;font-size:14px;color:#475569;line-height:1.7;">
            Congratulations! You have been shortlisted for an AI-powered L1 HR interview
            for the position of <strong>{job_title}</strong>.
            Please click the button below to join your interview at your convenience.
          </p>

          <!-- CTA Button -->
          <table cellpadding="0" cellspacing="0" style="margin:0 0 28px;">
            <tr><td style="background:#2563EB;border-radius:9px;">
              <a href="{join_url}"
                 style="color:#ffffff;font-size:15px;font-weight:600;
                        text-decoration:none;display:block;padding:14px 32px;">
                Join Interview &rarr;
              </a>
            </td></tr>
          </table>

          <!-- Info box -->
          <table cellpadding="0" cellspacing="0" width="100%"
                 style="background:#eff6ff;border:1px solid #bfdbfe;
                        border-radius:9px;margin:0 0 24px;">
            <tr><td style="padding:16px 20px;">
              <p style="margin:0 0 8px;font-size:13px;color:#1e3a5f;font-weight:600;">
                Before you begin:
              </p>
              <ul style="margin:0;padding-left:18px;font-size:13px;
                         color:#475569;line-height:2.0;">
                <li>Ensure you are in a <strong>quiet environment</strong></li>
                <li>Allow <strong>microphone access</strong> in your browser</li>
                <li>Use a <strong>laptop or desktop</strong> for best experience</li>
                <li>The interview takes approximately <strong>15–20 minutes</strong></li>
              </ul>
            </td></tr>
          </table>

          <p style="margin:0;font-size:12px;color:#94a3b8;">
            &#9201; This link expires in <strong>{expire_hours} hours</strong>.
            If you need to reschedule, please contact HR.
          </p>
        </td></tr>

        <!-- Footer -->
        <tr><td style="background:#f8fafc;border-top:1px solid #e2e8f0;padding:18px 36px;">
          <p style="margin:0;font-size:12px;color:#94a3b8;text-align:center;">
            This is an automated message from the AI Interview System.
            Please do not reply to this email.
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""
