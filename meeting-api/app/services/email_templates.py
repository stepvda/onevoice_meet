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

    body_html = f"""
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
