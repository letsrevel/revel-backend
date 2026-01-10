{% load i18n %}📝 {% blocktranslate with name=context.submitter_name questionnaire=context.questionnaire_name %}**{{ name }}** submitted the questionnaire **{{ questionnaire }}**.{% endblocktranslate %}

**{% trans "Submission Details:" %}**
- {% trans "Submitted by:" %} {{ context.submitter_name }} ({{ context.submitter_email }})
- {% trans "Questionnaire:" %} {{ context.questionnaire_name }}
- {% trans "Organization:" %} {{ context.organization_name }}
{% if context.event_name %}- {% trans "For Event:" %} {{ context.event_name }}{% endif %}

[{% trans "Review Submission" %}]({{ context.submission_url }})
