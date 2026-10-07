"""The document review's topics as shipped: UNICEF Lebanon's programmes, a few subtopics each and their tags,
and "Other" (a tag the AI invents becomes Other). Administrators change them in the admin (Document review
topics); the change applies to documents analysed afterwards. Rows already there are left as they are."""

from django.db import migrations

TOPICS = {
    "Education": {
        "Access to learning": ["Out-of-school children", "Enrolment and retention", "Non-formal education"],
        "Quality of learning": ["Teachers and teaching", "Learning outcomes", "Curriculum and materials"],
        "Education system": ["School infrastructure", "Education financing", "Education data"],
    },
    "Child Protection": {
        "Violence and abuse": ["Violence against children", "Child labour", "Child marriage"],
        "Services": ["Case management", "Psychosocial support", "Alternative care"],
        "Justice and systems": ["Justice for children", "Birth registration", "Child protection workforce"],
    },
    "Health & Nutrition": {
        "Health services": ["Immunization", "Primary health care", "Maternal and newborn health"],
        "Nutrition": ["Malnutrition", "Infant and young child feeding", "Micronutrients"],
        "Health system": ["Medicines and supplies", "Health workforce", "Health financing"],
    },
    "WASH": {
        "Water": ["Water supply", "Water quality", "Water establishments"],
        "Sanitation and hygiene": ["Sanitation", "Wastewater", "Hygiene promotion"],
        "Climate and environment": ["Climate resilience", "Solar energy", "Solid waste"],
    },
    "Social Protection & Inclusion": {
        "Cash and social assistance": ["Cash transfers", "Child grant", "Targeting"],
        "Inclusion": ["Children with disabilities", "Refugees and migrants", "Poverty and deprivation"],
        "Social protection system": ["Social protection policy", "Registry and payment systems"],
    },
    "Adolescents & Youth": {
        "Skills and employment": ["Skills training", "Employment and livelihoods", "Entrepreneurship"],
        "Participation": ["Youth engagement", "Volunteering", "Social cohesion"],
        "Wellbeing": ["Mental health", "Life skills"],
    },
    "Gender": {
        "Gender equality": ["Gender-based violence", "Women and girls' empowerment", "Gender mainstreaming"],
        "Protection from sexual exploitation": ["Protection from sexual exploitation and abuse"],
    },
    "Emergency/Humanitarian response": {
        "Preparedness": ["Contingency planning", "Prepositioned supplies", "Early warning"],
        "Response": ["Displacement and shelters", "Emergency supplies", "Access constraints"],
        "Recovery": ["Rehabilitation", "Return of displaced people"],
    },
    "Partnerships & Funding": {
        "Funding": ["Funding gaps", "Donor funding", "Flexible funding"],
        "Partners": ["Implementing partners", "Government partnership", "Partner capacity"],
        "Coordination": ["Sector coordination", "Inter-agency work"],
    },
    "Monitoring & Data": {
        "Monitoring": ["Field monitoring", "Third-party monitoring", "Programme assurance"],
        "Data and evidence": ["Data quality", "Data systems", "Evaluations and studies"],
        "Accountability": ["Accountability to affected populations", "Feedback and complaints"],
    },
}
OTHER = "Other"


def seed(apps, schema_editor):
    Programme = apps.get_model("knowledge", "TopicProgramme")
    Subtopic = apps.get_model("knowledge", "TopicSubtopic")
    Topic = apps.get_model("knowledge", "Topic")
    for p_order, (programme_name, subtopics) in enumerate(TOPICS.items()):
        programme, _ = Programme.objects.get_or_create(name=programme_name, defaults={"order": p_order})
        for s_order, (subtopic_name, tags) in enumerate(subtopics.items()):
            subtopic, _ = Subtopic.objects.get_or_create(
                programme=programme, name=subtopic_name, defaults={"order": s_order}
            )
            for t_order, tag in enumerate(tags):
                Topic.objects.get_or_create(subtopic=subtopic, name=tag, defaults={"order": t_order})
    other, _ = Programme.objects.get_or_create(name=OTHER, defaults={"order": 999})
    subtopic, _ = Subtopic.objects.get_or_create(programme=other, name=OTHER)
    Topic.objects.get_or_create(subtopic=subtopic, name=OTHER)


class Migration(migrations.Migration):
    dependencies = [("knowledge", "0004_document_review")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
