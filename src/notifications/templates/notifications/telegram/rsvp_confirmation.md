{% load i18n %}{% if context.user_name %}
{% blocktranslate with user=context.user_name event=context.event_name response=context.response %}<b>{{ user }}</b> has RSVP'd <b>{{ response }}</b> for <b>{{ event }}</b>.{% endblocktranslate %}

<b>{% trans "RSVP Details:" %}</b>
• {% trans "User:" %} {{ context.user_name }}
• {% trans "Response:" %} {{ context.response }}
{% if context.rsvp_note %}• {% trans "Note:" %} {{ context.rsvp_note }}{% endif %}

{% else %}
✅ {% blocktranslate with event=context.event_name response=context.response %}Your RSVP (<b>{{ response }}</b>) for <b>{{ event }}</b> confirmed!{% endblocktranslate %}

<b>{% trans "Event Details:" %}</b>
📅 {{ context.event_start_formatted }}
{% if context.event_location %}📍 {{ context.event_location }}{% endif %}

<b>{% trans "Your Response:" %}</b>
• {{ context.response }}
{% if context.guest_count %}• {% trans "Guests:" %} {{ context.guest_count }}{% endif %}
{% if context.dietary_restrictions %}• {% trans "Dietary restrictions:" %} {{ context.dietary_restrictions }}{% endif %}
{% endif %}

<a href="{{ context.event_url }}">{% trans "View Event" %}</a>
