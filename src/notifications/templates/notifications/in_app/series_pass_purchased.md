{% load i18n %}{% blocktranslate with pass_name=context.pass_name series=context.series_name %}Your series pass **{{ pass_name }}** for **{{ series }}** is now active.{% endblocktranslate %}

**{% trans "Pass Details:" %}**
- {% trans "Pass:" %} {{ context.pass_name }}
- {% trans "Series:" %} {{ context.series_name }}
- {% trans "Organization:" %} {{ context.organization_name }}
- {% trans "Events covered:" %} {{ context.event_count }}
- {% trans "Price paid:" %} {{ context.price_paid }} {{ context.currency }}
