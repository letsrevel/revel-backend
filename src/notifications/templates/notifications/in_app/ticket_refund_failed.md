{% load i18n %}{% blocktranslate with amount=context.refund_amount event=context.event_name %}The refund of {{ amount }} for {{ event }} did not go through at the payment provider.{% endblocktranslate %}

{% trans "The money was not returned to you. Contact the organizer if you have questions." %}
