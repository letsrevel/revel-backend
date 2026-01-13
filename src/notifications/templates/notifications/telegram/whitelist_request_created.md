{% load i18n %}**{% trans "New Verification Request" %}**

**{{ context.requester_name }}** {% trans "requested verification for" %} **{{ context.organization_name }}**

{% trans "Email:" %} {{ context.requester_email }}
{% trans "Matched Entries:" %} {{ context.matched_entries_count }}
{% if context.request_message %}
{% trans "Message:" %}
> {{ context.request_message }}
{% endif %}

[{% trans "Review Request" %}]({{ context.frontend_url }})
