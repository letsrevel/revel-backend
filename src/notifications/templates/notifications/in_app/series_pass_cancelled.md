{% load i18n %}{% blocktranslate with pass_name=context.pass_name series=context.series_name %}Your series pass **{{ pass_name }}** for **{{ series }}** has been cancelled.{% endblocktranslate %}

**{% trans "Cancellation Details:" %}**
- {% trans "Pass:" %} {{ context.pass_name }}
- {% trans "Series:" %} {{ context.series_name }}
- {% trans "Organization:" %} {{ context.organization_name }}
- {% trans "Tickets cancelled:" %} {{ context.cancelled_ticket_count }}
{% if context.reason %}- {% trans "Reason:" %} {{ context.reason }}
{% endif %}
{% if context.refunded_total != "0.00" %}
{% if context.holder_name %}💰 {% blocktranslate with amount=context.refunded_total currency=context.currency %}A refund of **{{ amount }} {{ currency }}** has been issued to the pass holder.{% endblocktranslate %}{% else %}💰 {% blocktranslate with amount=context.refunded_total currency=context.currency %}A refund of **{{ amount }} {{ currency }}** will be processed within 5-10 business days.{% endblocktranslate %}{% endif %}
{% endif %}