"""
HTML email templates for meet.witysk.org.

Visual language: dark grey → very dark blue, matching the in-app
primary-500 (#1E3A5F) → primary-900 (#0E1E33) palette and the accent green
(#4CAF50). Inline CSS only — Gmail/Outlook strip <style> blocks. No
external assets. f-string substitution.
"""
from html import escape
from typing import Optional


def _wrap(*, preview: str, body_html: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>meet.witysk.org</title>
</head>
<body style="margin:0;padding:0;background:#0b0b0f;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#e6ebf3;">
  <span style="display:none!important;visibility:hidden;opacity:0;color:transparent;height:0;width:0;overflow:hidden;mso-hide:all;">{escape(preview)}</span>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#0b0b0f;">
    <tr>
      <td align="center" style="padding:32px 16px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;background:#12253E;border:1px solid #1A3354;border-radius:12px;overflow:hidden;">
          <tr>
            <td style="padding:0;background:linear-gradient(135deg,#0E1E33 0%,#162C49 50%,#1E3A5F 100%);">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td style="padding:28px 32px 24px 32px;">
                    <div style="font-size:14px;font-weight:600;letter-spacing:0.06em;color:#7f9dbf;text-transform:uppercase;">meet.witysk.org</div>
                    <div style="height:3px;width:42px;background:#4CAF50;margin-top:8px;border-radius:2px;"></div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:24px 32px 32px 32px;line-height:1.55;font-size:15px;color:#dde6f2;">
              {body_html}
            </td>
          </tr>
          <tr>
            <td style="padding:18px 32px;background:#0E1E33;border-top:1px solid #1A3354;font-size:12px;color:#7f9dbf;line-height:1.5;">
              You received this because someone invited you to a meet.witysk.org meeting.
              <br>
              meet.witysk.org is a self-hosted video conferencing service for the TI One Voice community.
            </td>
          </tr>
        </table>
        <div style="max-width:600px;margin:14px auto 0 auto;font-size:11px;color:#4f73a0;text-align:center;">
          If you did not expect this email, you can safely ignore it.
        </div>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _btn(label: str, href: str) -> str:
    return f"""<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:24px 0 16px 0;">
  <tr><td style="border-radius:8px;background:#4CAF50;">
    <a href="{escape(href, quote=True)}" style="display:inline-block;padding:14px 28px;font-size:15px;font-weight:600;color:#ffffff;text-decoration:none;border-radius:8px;background:#4CAF50;">{escape(label)}</a>
  </td></tr>
</table>"""


def meeting_invite(
    *,
    inviter_name: str,
    inviter_email: Optional[str],
    meeting_title: str,
    join_url: str,
    personal_message: Optional[str] = None,
    branding_url: Optional[str] = None,
) -> tuple[str, str, str]:
    """Returns (subject, html, plaintext) for a meeting invite."""
    inviter_label = inviter_name or inviter_email or "Someone"
    subject = f"{inviter_label} invited you to: {meeting_title}"

    note_block = ""
    if personal_message:
        note_block = f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0 20px 0;background:#0E1E33;border-left:3px solid #4CAF50;border-radius:4px;">
  <tr><td style="padding:14px 16px;color:#dde6f2;font-style:italic;font-size:14px;line-height:1.5;">
    "{escape(personal_message)}"
  </td></tr>
</table>"""

    branding_block = ""
    if branding_url:
        branding_block = f"""
<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 16px 0;">
  <tr><td>
    <img src="{escape(branding_url, quote=True)}" alt="" width="96" height="96"
         style="display:block;width:96px;height:96px;object-fit:cover;border-radius:8px;border:1px solid #1A3354;">
  </td></tr>
</table>"""

    body_html = f"""
{branding_block}
<h1 style="margin:0 0 12px 0;font-size:22px;font-weight:600;color:#ffffff;">You're invited to a meeting</h1>
<p style="margin:0 0 14px 0;color:#b6c7dd;">
  <b style="color:#dde6f2;">{escape(inviter_label)}</b> invited you to join
  <b style="color:#dde6f2;">{escape(meeting_title)}</b> on meet.witysk.org.
</p>
{note_block}
{_btn("Join the meeting", join_url)}
<p style="margin:8px 0 0 0;font-size:13px;color:#7f9dbf;">
  Or paste this URL into your browser:
  <br>
  <a href="{escape(join_url, quote=True)}" style="color:#7f9dbf;word-break:break-all;">{escape(join_url)}</a>
</p>
<hr style="border:0;border-top:1px solid #1A3354;margin:22px 0;">
<p style="margin:0;font-size:13px;color:#7f9dbf;">
  No account is required to join. Just enter your name in the lobby and you're in.
  Audio, video, screenshare, and chat work directly in your browser.
</p>"""

    text_lines = [
        f"{inviter_label} invited you to: {meeting_title}",
        "",
    ]
    if personal_message:
        text_lines += [f"Note: {personal_message}", ""]
    text_lines += [
        "Join the meeting:",
        join_url,
        "",
        "No account required — just enter your name in the lobby.",
        "",
        "— meet.witysk.org",
    ]
    return subject, _wrap(preview=subject, body_html=body_html), "\n".join(text_lines)


def account_welcome(*, name: str | None, username: str, signup_url: str) -> tuple[str, str, str]:
    """Sent right after a successful native sign-up. Same chrome as the invite
    template (dark gradient, accent green CTA) so the brand stays consistent."""
    label = (name or username).strip()
    subject = "Welcome to meet.witysk.org"
    body_html = f"""
<h1 style="margin:0 0 12px 0;font-size:22px;font-weight:600;color:#ffffff;">Welcome, {escape(label)}.</h1>
<p style="margin:0 0 14px 0;color:#b6c7dd;">
  Your meet.witysk.org account is ready. You're on a free <b style="color:#dde6f2;">10-day trial</b>
  during which you can create unlimited meetings — after that, redeem a voucher
  or subscribe to keep meeting-creation rights. Joining meetings, audio Café,
  chat and recording playback always remain free.
</p>
<p style="margin:0 0 14px 0;color:#b6c7dd;">
  Sign in at any time at:
</p>
{_btn("Open meet.witysk.org", signup_url)}
<hr style="border:0;border-top:1px solid #1A3354;margin:22px 0;">
<p style="margin:0;font-size:13px;color:#7f9dbf;">
  If you didn't create this account, just ignore this email — the account stays
  inert until someone signs in to it.
</p>"""
    text_lines = [
        f"Welcome, {label}.",
        "",
        "Your meet.witysk.org account is ready.",
        "You're on a free 10-day trial during which you can create unlimited meetings.",
        "",
        f"Open: {signup_url}",
        "",
        "— meet.witysk.org",
    ]
    return subject, _wrap(preview=subject, body_html=body_html), "\n".join(text_lines)


def login_otp(*, name: str | None, username: str, code: str, expires_in_minutes: int) -> tuple[str, str, str]:
    """One-time login code (email-based 2FA). The code is single-use and
    short-lived; same-style typography as password_reset for consistency."""
    label = (name or username).strip()
    subject = "Your meet.witysk.org login code"
    body_html = f"""
<h1 style="margin:0 0 12px 0;font-size:22px;font-weight:600;color:#ffffff;">Your login code</h1>
<p style="margin:0 0 14px 0;color:#b6c7dd;">
  Hi {escape(label)} — use this code to finish signing in to meet.witysk.org.
  It expires in <b style="color:#dde6f2;">{expires_in_minutes} minutes</b>
  and can only be used once.
</p>
<p style="margin:18px 0;font-size:32px;font-weight:700;letter-spacing:8px;color:#ffffff;font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;">{escape(code)}</p>
<hr style="border:0;border-top:1px solid #1A3354;margin:22px 0;">
<p style="margin:0;font-size:13px;color:#7f9dbf;">
  If you didn't try to sign in, you can safely ignore this email — your
  account stays as it is.
</p>"""
    text_lines = [
        f"Hi {label} — your meet.witysk.org login code:",
        code,
        "",
        f"Expires in {expires_in_minutes} minutes. One-time use.",
        "If you didn't try to sign in, ignore this email.",
        "",
        "— meet.witysk.org",
    ]
    return subject, _wrap(preview=subject, body_html=body_html), "\n".join(text_lines)


def password_reset(*, name: str | None, username: str, reset_url: str, expires_in_minutes: int) -> tuple[str, str, str]:
    """Single-use password-reset link. Token is in the URL fragment; the page
    handler sends it to the API to actually reset the password."""
    label = (name or username).strip()
    subject = "Reset your meet.witysk.org password"
    body_html = f"""
<h1 style="margin:0 0 12px 0;font-size:22px;font-weight:600;color:#ffffff;">Reset your password</h1>
<p style="margin:0 0 14px 0;color:#b6c7dd;">
  Hi {escape(label)} — we received a request to reset the password on your
  meet.witysk.org account. Click the button below to choose a new one.
  This link expires in <b style="color:#dde6f2;">{expires_in_minutes} minutes</b>
  and can only be used once.
</p>
{_btn("Reset password", reset_url)}
<p style="margin:8px 0 0 0;font-size:13px;color:#7f9dbf;">
  Or paste this URL into your browser:
  <br>
  <a href="{escape(reset_url, quote=True)}" style="color:#7f9dbf;word-break:break-all;">{escape(reset_url)}</a>
</p>
<hr style="border:0;border-top:1px solid #1A3354;margin:22px 0;">
<p style="margin:0;font-size:13px;color:#7f9dbf;">
  If you didn't request a reset, you can safely ignore this email — your
  password stays unchanged. The link expires automatically.
</p>"""
    text_lines = [
        f"Hi {label} — reset your meet.witysk.org password:",
        reset_url,
        "",
        f"Expires in {expires_in_minutes} minutes. One-time use.",
        "If you didn't request this, ignore the email.",
        "",
        "— meet.witysk.org",
    ]
    return subject, _wrap(preview=subject, body_html=body_html), "\n".join(text_lines)
