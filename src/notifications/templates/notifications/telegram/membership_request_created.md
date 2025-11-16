{% load i18n %}**{% trans "New Membership Request" %}**

**{{ context.requester_name }}** {% trans "requested to join" %} **{{ context.organization_name }}**

{% trans "Email:" %} {{ context.requester_email }}
{% if context.request_message %}
{% trans "Message:" %}
> {{ context.request_message }}
{% endif %}

[{% trans "View Request" %}]({{ context.frontend_url }})
