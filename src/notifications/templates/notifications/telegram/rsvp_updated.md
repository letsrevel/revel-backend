{% load i18n %}{% if context.user_name %}
{% if context.old_response != context.new_response %}{% blocktranslate with user=context.user_name event=context.event_name old=context.old_response new=context.new_response %}<b>{{ user }}</b> changed RSVP from {{ old }} to <b>{{ new }}</b> for <b>{{ event }}</b>.{% endblocktranslate %}{% else %}{% blocktranslate with user=context.user_name event=context.event_name %}<b>{{ user }}</b> updated their RSVP note for <b>{{ event }}</b>.{% endblocktranslate %}{% endif %}

<b>{% trans "Updated RSVP:" %}</b>
• {% trans "User:" %} {{ context.user_name }}
• {% trans "Previous:" %} {{ context.old_response }}
• {% trans "New:" %} {{ context.new_response }}
{% if context.rsvp_note %}• {% trans "Note:" %} {{ context.rsvp_note }}{% endif %}

{% else %}
🔄 {% blocktranslate with event=context.event_name old=context.old_response new=context.new_response %}RSVP for <b>{{ event }}</b> updated from {{ old }} to <b>{{ new }}</b>.{% endblocktranslate %}

<b>{% trans "Event Details:" %}</b>
📅 {{ context.event_start_formatted }}
{% if context.event_location %}📍 {{ context.event_location }}{% endif %}

<b>{% trans "Updated Response:" %}</b> {{ context.new_response }}
{% if context.guest_count %}• {% trans "Guests:" %} {{ context.guest_count }}{% endif %}
{% endif %}

<a href="{{ context.event_url }}">{% trans "View Event" %}</a>
