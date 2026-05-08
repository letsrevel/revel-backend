{% load i18n %}{% blocktranslate with sender=context.sender_email org=context.organization_name %}**{{ sender }}** sent a message to **{{ org }}**.{% endblocktranslate %}

{% if context.subject %}**{% trans "Subject:" %}** {{ context.subject }}
{% endif %}
{% if context.message_preview %}> {{ context.message_preview }}
{% endif %}

[{% trans "View Message" %}]({{ context.admin_url }})
