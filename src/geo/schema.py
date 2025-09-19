from ninja import ModelSchema

from geo.models import City


class CitySchema(ModelSchema):
    class Meta:
        model = City
        fields = ["id", "name", "country", "admin_name"]
