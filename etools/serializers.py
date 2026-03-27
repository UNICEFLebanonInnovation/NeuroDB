
from rest_framework import serializers
from etools.models import PartnerOrganization


class PartnerOrganizationSerializer(serializers.ModelSerializer):

    class Meta:
        model = PartnerOrganization
        fields = (
            'id',
            'comments'
        )
