{% load i18n %}**{% trans "New Verification Request" %}**

{% blocktranslate with name=context.requester_name org=context.organization_name %}**{{ name }}** requested verification for **{{ org }}**.{% endblocktranslate %}

{% trans "Email:" %} {{ context.requester_email }}
{% trans "Matched Entries:" %} {{ context.matched_entries_count }}
{% if context.request_message %}
{% trans "Message:" %}
> {{ context.request_message }}
{% endif %}

[{% trans "Review Request" %}]({{ context.frontend_url }})
