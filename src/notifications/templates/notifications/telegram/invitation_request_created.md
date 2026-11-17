{% load i18n %}**{% trans "New Invitation Request" %}**

{% blocktranslate with email=context.requester_email event=context.event_name %}**{{ email }}** requested an invitation to **{{ event }}**.{% endblocktranslate %}

{% if context.requester_name %}{% trans "Name:" %} {{ context.requester_name }}{% endif %}
{% if context.request_message %}
{% trans "Message:" %}
> {{ context.request_message }}
{% endif %}

[{% trans "View Request" %}]({{ context.frontend_url }})
