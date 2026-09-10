"""Pure utilities for the questionnaires app.

Helpers here hold policy that both this app and its consumers (the events app's
eligibility gates and submission services) must agree on. They perform no
queries and — like the rest of ``questionnaires`` — must never import from
``events``: policy inputs are passed in as plain values.
"""
