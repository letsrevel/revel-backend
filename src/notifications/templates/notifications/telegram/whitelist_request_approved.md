{% load i18n %}**{% trans "Verification Approved" %}**

{% blocktranslate with org=context.organization_name %}Your verification for **{{ org }}** has been approved. You now have full access.{% endblocktranslate %}

<a href="{{ context.frontend_url }}">{% trans "View Organization" %}</a>
