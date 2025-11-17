{% load i18n %}**{{ context.requester_name }}** {% trans "requested to join" %} **{{ context.organization_name }}**

{% if context.request_message %}
> {{ context.request_message }}
{% endif %}

[{% trans "View Request" %}]({{ context.frontend_url }})
