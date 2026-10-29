{% load i18n %}⚠️ {% if context.outcome == "closed" %}{% blocktranslate with status=context.status_label amount=context.amount currency=context.currency %}A payment dispute of {{ amount }} {{ currency }} has been closed: {{ status }}.{% endblocktranslate %}{% else %}{% blocktranslate with amount=context.amount currency=context.currency %}A payment dispute of {{ amount }} {{ currency }} has been opened.{% endblocktranslate %}{% endif %}

<b>{% trans "Dispute Information:" %}</b>
• {% trans "Reason:" %} {{ context.reason_label }}
• {% trans "Status:" %} {{ context.status_label }}
{% if context.event_name %}• {% trans "Event:" %} {{ context.event_name }}
{% endif %}{% if context.evidence_due_by and context.outcome != "closed" %}• {% trans "Evidence due by:" %} <b>{{ context.evidence_due_by }}</b>
{% endif %}{% if context.outcome != "closed" %}
{% trans "Disputes are answered in your Stripe Dashboard. Submit your evidence before the deadline above." %}
{{ context.dashboard_url }}{% endif %}
