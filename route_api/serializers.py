from rest_framework import serializers

from .services.geo import is_valid_coordinate


class LocationField(serializers.Field):
    """
    A US location: ``"City, ST"``, ``"lat,lng"``, a free-text place, or an
    object ``{"lat": 40.71, "lon": -74.0}`` (``lng`` also accepted).
    """

    default_error_messages = {
        # Braces are doubled: DRF formats these messages with str.format().
        "invalid": 'Provide "City, ST", "lat,lng", or an object {{"lat": .., "lon": ..}}.',
        "blank": "This field may not be blank.",
        "max_length": "Ensure this field has no more than 200 characters.",
        "coordinates": "lat must be -90..90 and lon -180..180 (decimal degrees).",
    }

    def to_internal_value(self, data):
        if isinstance(data, str):
            value = data.strip()
            if not value:
                self.fail("blank")
            if len(value) > 200:
                self.fail("max_length")
            return value
        if isinstance(data, dict):
            lat = data.get("lat")
            lon = data.get("lon", data.get("lng"))
            if isinstance(lat, bool) or isinstance(lon, bool) or not is_valid_coordinate(lat, lon):
                self.fail("coordinates")
            return {"lat": float(lat), "lon": float(lon)}
        self.fail("invalid")

    def to_representation(self, value):
        return value


class RouteRequestSerializer(serializers.Serializer):
    start = LocationField(help_text='e.g. "New York, NY" or "40.7128,-74.0060"')
    finish = LocationField(help_text='e.g. "Los Angeles, CA"')
    corridor_miles = serializers.FloatField(
        required=False, min_value=1, max_value=50,
        help_text="Max straight-line miles between a station's (city-level) point and the route. Default 15.",
    )
    min_location_confidence = serializers.ChoiceField(
        choices=["low", "medium", "high"], required=False, default="low",
        help_text='Only use stations at least this confident. "high" = the address highway is on the route.',
    )
