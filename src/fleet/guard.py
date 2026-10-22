"""Detects pages that need a human (RULES.md §2). Fleet pauses; it never tries to get past them."""

import logging

log = logging.getLogger("fleet.guard")

# Runs in the page. Returns a short reason string, or null when the page looks normal.
DETECT_JS = r"""
(() => {
  // Only visible, interactive challenges count: invisible reCAPTCHA v3 badges sit on many normal pages.
  const visible = el => { const r = el.getBoundingClientRect(); return r.width > 40 && r.height > 40; };
  const frames = [...document.querySelectorAll('iframe')]
    .filter(visible)
    .map(f => (f.src || '').toLowerCase())
    .filter(src => !src.includes('size=invisible'));
  const markers = [
    ['recaptcha', 'reCAPTCHA'], ['hcaptcha', 'hCaptcha'], ['challenges.cloudflare.com', 'Cloudflare check'],
    ['arkoselabs', 'Arkose challenge'], ['funcaptcha', 'Arkose challenge'], ['captcha-delivery', 'DataDome check'],
  ];
  for (const [needle, name] of markers) if (frames.some(src => src.includes(needle))) return name;
  if (document.querySelector('#challenge-form, #challenge-stage')) return 'Cloudflare check';
  const title = (document.title || '').toLowerCase();
  if (title.includes('just a moment') || title.includes('attention required')) return 'Cloudflare check';
  const text = (document.body ? document.body.innerText : '').slice(0, 4000).toLowerCase();
  const phrases = ['verify you are human', 'verify that you are human', "confirm you're not a robot",
                   'are you a robot', 'unusual traffic from your computer', 'press and hold'];
  for (const p of phrases) if (text.includes(p)) return 'human verification page';
  if (document.querySelector('input[autocomplete="one-time-code"]')) return '2FA code prompt';
  return null;
})()
"""


async def detect_challenge_with(evaluate) -> str | None:
    """Same check through any `await evaluate(js)` (the fast browser engine's page)."""
    try:
        return await evaluate(DETECT_JS) or None
    except Exception as e:  # the page may be navigating
        log.debug("challenge check skipped: %s", e)
        return None


async def detect_challenge(browser_session) -> str | None:
    """Evaluate DETECT_JS in the active tab. Any failure counts as 'nothing detected'."""
    try:
        cdp = await browser_session.get_or_create_cdp_session(focus=False)
        result = await cdp.cdp_client.send.Runtime.evaluate(
            params={"expression": DETECT_JS, "returnByValue": True}, session_id=cdp.session_id
        )
        return (result.get("result") or {}).get("value") or None
    except Exception as e:  # the page may be navigating
        log.debug("challenge check skipped: %s", e)
        return None
