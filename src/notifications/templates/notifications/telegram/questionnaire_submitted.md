{% load i18n %}📝 {% blocktranslate %}<b>New Questionnaire Submission</b>{% endblocktranslate %}

{% blocktranslate with name=context.submitter_name questionnaire=context.questionnaire_name %}<b>{{ name }}</b> submitted the questionnaire <b>{{ questionnaire }}</b>.{% endblocktranslate %}

{% trans "Submitted by:" %} {{ context.submitter_email }}
{% trans "Organization:" %} {{ context.organization_name }}
{% if context.event_name %}{% trans "For Event:" %} {{ context.event_name }}{% endif %}

<a href="{{ context.submission_url }}">{% trans "Review Submission" %}</a>
