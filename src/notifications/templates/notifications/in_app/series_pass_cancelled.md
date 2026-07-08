{% load i18n %}{% blocktranslate with pass_name=context.pass_name series=context.series_name %}Your series pass **{{ pass_name }}** for **{{ series }}** has been cancelled.{% endblocktranslate %}

**{% trans "Cancellation Details:" %}**
- {% trans "Pass:" %} {{ context.pass_name }}
- {% trans "Series:" %} {{ context.series_name }}
- {% trans "Organization:" %} {{ context.organization_name }}
- {% trans "Tickets cancelled:" %} {{ context.cancelled_ticket_count }}
- {% trans "Refunded:" %} {{ context.refunded_total }} {{ context.currency }}
{% if context.reason %}- {% trans "Reason:" %} {{ context.reason }}
{% endif %}