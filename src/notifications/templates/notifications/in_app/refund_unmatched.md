{% load i18n %}{% blocktranslate with amount=context.refund_amount currency=context.currency intent=context.payment_intent_id %}A refund of {{ amount }} {{ currency }} arrived from Stripe on payment {{ intent }}, but Revel could not tell which ticket it belongs to. No ticket was cancelled and no seat was freed.{% endblocktranslate %}

**{% trans "It could apply to any of these tickets:" %}**
{% for candidate in context.candidates %}- {{ candidate.event_name }}{% if candidate.seat_label %} — {% blocktranslate with seat=candidate.seat_label %}seat {{ seat }}{% endblocktranslate %}{% endif %} — {{ candidate.amount }} {{ context.currency }} — {{ candidate.holder_email }}
{% endfor %}

{% trans "Check the refund in Stripe, then cancel the matching ticket in Revel. Refunds issued through Revel are always applied automatically." %}

[{% trans "Review these tickets in Revel" %}]({{ context.resolve_url }})

{% trans "Refund ID:" %} {{ context.refund_id }}
