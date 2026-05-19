{% load i18n %}🎉 **{% trans "Spot Available!" %}** {% trans "for" %} **{{ context.event_name }}**

{% if context.is_cutoff_batch %}{% trans "Final call — the waitlist has opened to everyone." %}{% else %}{% trans "You've been selected from the waitlist." %}{% endif %}

⏰ {% trans "Claim before" %} {{ context.expires_at_formatted }} ({{ context.time_remaining_formatted }}).

[{% trans "Claim your spot" %}]({{ context.event_url }})
