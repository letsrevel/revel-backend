{% load i18n %}⚠️ {% if context.outcome == "closed" %}{% blocktranslate with status=context.status_label amount=context.amount currency=context.currency %}A payment dispute of {{ amount }} {{ currency }} has been closed: {{ status }}.{% endblocktranslate %}{% else %}{% blocktranslate with amount=context.amount currency=context.currency %}A payment dispute of {{ amount }} {{ currency }} has been opened.{% endblocktranslate %}{% endif %}

**{% trans "Dispute Information:" %}**
- {% trans "Reason:" %} {{ context.reason_label }}
- {% trans "Status:" %} {{ context.status_label }}
{% if context.event_name %}- {% trans "Event:" %} {{ context.event_name }}
{% endif %}{% if context.evidence_due_by and context.outcome != "closed" %}- {% trans "Evidence due by:" %} **{{ context.evidence_due_by }}**
{% endif %}{% if context.outcome != "closed" %}
[{% trans "Open the dispute in Stripe" %}]({{ context.dashboard_url }}){% endif %}
