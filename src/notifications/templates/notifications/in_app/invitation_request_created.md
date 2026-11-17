{% load i18n %}{% blocktranslate with email=context.requester_email event=context.event_name %}**{{ email }}** requested an invitation to **{{ event }}**.{% endblocktranslate %}

{% if context.request_message %}
> {{ context.request_message }}
{% endif %}

[{% trans "View Request" %}]({{ context.frontend_url }})
