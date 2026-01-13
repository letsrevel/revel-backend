{% load i18n %}{% blocktranslate with org=context.organization_name %}Your verification for **{{ org }}** has been approved{% endblocktranslate %}

[{% trans "View Organization" %}]({{ context.frontend_url }})
