{% load i18n %}**{% trans "New Contact Message" %}**

{% blocktranslate with sender=context.sender_email org=context.organization_name %}**{{ sender }}** sent a message to **{{ org }}**.{% endblocktranslate %}

{% trans "Open Revel to read the full message." %}

[{% trans "View Message" %}]({{ context.admin_url }})
