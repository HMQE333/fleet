"""Fast browser engine: Jev picks one observed action per step; code owns execution.

Adapted from jev-ultrafast and JevBrowser (MIT, see NOTICE.md). It works on structured page
state instead of screenshots and makes one small decision call per step, so simple browser
work runs in seconds for fractions of a cent. Fleet wraps it with the same safety layer as the
full browser agent (captcha/login hand-off, approvals, cancel, domain limits).
"""
