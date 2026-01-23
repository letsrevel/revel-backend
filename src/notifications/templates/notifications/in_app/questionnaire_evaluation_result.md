{% load i18n %}{% if context.evaluation_status == "APPROVED" %}{% if context.event_name %}✅ {% blocktranslate with questionnaire=context.questionnaire_name event=context.event_name %}Your questionnaire **{{ questionnaire }}** for **{{ event }}** has been approved!{% endblocktranslate %}{% else %}✅ {% blocktranslate with questionnaire=context.questionnaire_name %}Your questionnaire **{{ questionnaire }}** has been approved!{% endblocktranslate %}{% endif %}{% else %}{% if context.event_name %}❌ {% blocktranslate with questionnaire=context.questionnaire_name event=context.event_name %}Your questionnaire **{{ questionnaire }}** for **{{ event }}** was not approved.{% endblocktranslate %}{% else %}❌ {% blocktranslate with questionnaire=context.questionnaire_name %}Your questionnaire **{{ questionnaire }}** was not approved.{% endblocktranslate %}{% endif %}{% endif %}

**{% trans "Details:" %}**
- {% trans "Questionnaire:" %} {{ context.questionnaire_name }}
- {% trans "Organization:" %} {{ context.organization_name }}
{% if context.event_name %}- {% trans "Event:" %} {{ context.event_name }}
{% endif %}- {% trans "Status:" %} {% if context.evaluation_status == "APPROVED" %}{% trans "Approved" %}{% else %}{% trans "Not Approved" %}{% endif %}
{% if context.evaluation_score %}- {% trans "Score:" %} {{ context.evaluation_score }}{% endif %}
{% if context.evaluation_comments %}
**{% trans "Evaluator Comments:" %}**
> {{ context.evaluation_comments }}
{% endif %}
{% if context.evaluation_status == "APPROVED" and context.event_url %}
⚠️ **{% trans "Important:" %}** {% if context.requires_ticket %}{% trans "Approval does not mean you are registered. Please secure your ticket to confirm your attendance." %}{% else %}{% trans "Approval does not mean you are registered. Please RSVP to confirm your attendance." %}{% endif %}
{% endif %}
{% if context.event_url %}
[{% if context.evaluation_status == "APPROVED" %}{% if context.requires_ticket %}{% trans "Get Ticket" %}{% else %}{% trans "RSVP Now" %}{% endif %}{% else %}{% trans "View Event" %}{% endif %}]({{ context.event_url }})
{% endif %}
