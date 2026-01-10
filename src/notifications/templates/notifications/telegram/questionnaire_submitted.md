{% load i18n %}📝 {% blocktranslate %}<b>New Questionnaire Submission</b>{% endblocktranslate %}

<b>{{ context.submitter_name }}</b> {% trans "submitted" %} <b>{{ context.questionnaire_name }}</b>

{% trans "Submitted by:" %} {{ context.submitter_email }}
{% trans "Organization:" %} {{ context.organization_name }}
{% if context.event_name %}{% trans "For Event:" %} {{ context.event_name }}{% endif %}

<a href="{{ context.submission_url }}">{% trans "Review Submission" %}</a>
