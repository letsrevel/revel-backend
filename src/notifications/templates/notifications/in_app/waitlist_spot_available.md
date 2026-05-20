{% load i18n %}🎉 **{% trans "Spot Available!" %}** {% trans "for" %} **{{ context.event_name }}**

{% if context.is_cutoff_batch %}{% trans "Final call — the waitlist has opened to everyone." %}{% else %}{% trans "You've been selected from the waitlist." %}{% endif %}

{% if context.expires_at_formatted %}⏰ {% trans "Claim before" %} {{ context.expires_at_formatted }}{% if context.time_remaining_formatted %} ({{ context.time_remaining_formatted }}){% endif %}.

{% endif %}[{% trans "Claim your spot" %}]({{ context.event_url }})
