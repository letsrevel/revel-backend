"""Signals defined by the accounts app.

Leaf module: it must import nothing from the app, so any module can import these signals without
creating an import cycle.
"""

from django.dispatch import Signal

# Sent by ``accounts.service.account.convert_guest`` (with ``.send``, not ``send_robust``) inside
# its transaction, so a receiver failure rolls the conversion back. Kwargs: ``user``.
guest_converted = Signal()
