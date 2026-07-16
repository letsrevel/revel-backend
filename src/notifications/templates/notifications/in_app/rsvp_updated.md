{% load i18n %}{% if context.user_name %}
{% if context.old_response != context.new_response %}{% blocktranslate with user=context.user_name event=context.event_name old=context.old_response new=context.new_response %}**{{ user }}** changed RSVP from **{{ old }}** to **{{ new }}** for **{{ event }}**.{% endblocktranslate %}{% else %}{% blocktranslate with user=context.user_name event=context.event_name %}**{{ user }}** updated their RSVP note for **{{ event }}**.{% endblocktranslate %}{% endif %}

**{% trans "Updated RSVP Details:" %}**
- {% trans "User:" %} {{ context.user_name }}
- {% trans "Previous Response:" %} {{ context.old_response }}
- {% trans "New Response:" %} {{ context.new_response }}
{% if context.rsvp_note %}- {% trans "Note:" %} {{ context.rsvp_note }}{% endif %}

{% else %}
{% blocktranslate with event=context.event_name old=context.old_response new=context.new_response %}Your RSVP for **{{ event }}** updated from **{{ old }}** to **{{ new }}**. 🔄{% endblocktranslate %}

**{% trans "Event Details:" %}**
- 📅 {{ context.event_start_formatted }}
{% if context.event_location %}- 📍 {{ context.event_location }}{% endif %}

**{% trans "Updated Response:" %}** {{ context.new_response }}
{% if context.guest_count %}- {% trans "Guests:" %} {{ context.guest_count }}{% endif %}
{% endif %}

[{% trans "View Event" %}]({{ context.event_url }})
