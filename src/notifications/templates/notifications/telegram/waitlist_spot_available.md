{% load i18n %}🎉 {% blocktranslate with event=context.event_name %}<b>Spot Available!</b> for <b>{{ event }}</b>{% endblocktranslate %}

{% if context.is_cutoff_batch %}{% trans "Final call — the waitlist has opened to everyone." %}{% else %}{% trans "You've been selected from the waitlist." %}{% endif %}

{% if context.expires_at_formatted %}⏰ {% trans "Claim before" %} {{ context.expires_at_formatted }}{% if context.time_remaining_formatted %} ({{ context.time_remaining_formatted }}){% endif %}.

{% endif %}<a href="{{ context.event_url }}">{% trans "Claim your spot" %}</a>
