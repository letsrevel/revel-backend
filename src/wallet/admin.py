"""Django admin configuration for wallet pass models."""

from django.contrib import admin
from unfold.admin import ModelAdmin

from wallet.models import WalletPassDevice, WalletPassRegistration, WalletPassUpdateLog


@admin.register(WalletPassDevice)
class WalletPassDeviceAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for wallet pass devices."""

    list_display = ["device_library_id_short", "platform", "created_at", "registration_count"]
    list_filter = ["platform", "created_at"]
    search_fields = ["device_library_id", "push_token"]
    readonly_fields = ["created_at", "updated_at"]
    ordering = ["-created_at"]

    @admin.display(description="Device ID")
    def device_library_id_short(self, obj: WalletPassDevice) -> str:
        """Show truncated device ID."""
        return f"{obj.device_library_id[:20]}..."

    @admin.display(description="Registrations")
    def registration_count(self, obj: WalletPassDevice) -> int:
        """Count of pass registrations for this device."""
        return obj.registrations.count()


@admin.register(WalletPassRegistration)
class WalletPassRegistrationAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for wallet pass registrations."""

    list_display = ["ticket", "device_short", "platform", "created_at"]
    list_filter = ["device__platform", "created_at"]
    search_fields = ["ticket__id", "ticket__user__email", "ticket__event__name"]
    readonly_fields = ["auth_token", "created_at", "updated_at"]
    raw_id_fields = ["ticket", "device"]
    ordering = ["-created_at"]

    @admin.display(description="Device")
    def device_short(self, obj: WalletPassRegistration) -> str:
        """Show truncated device ID."""
        return f"{obj.device.device_library_id[:12]}..."

    @admin.display(description="Platform")
    def platform(self, obj: WalletPassRegistration) -> str:
        """Show device platform."""
        return obj.device.get_platform_display()


@admin.register(WalletPassUpdateLog)
class WalletPassUpdateLogAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for wallet pass update logs."""

    list_display = ["update_type", "ticket", "device_short", "created_at"]
    list_filter = ["update_type", "created_at"]
    search_fields = ["ticket__id", "ticket__event__name"]
    readonly_fields = ["ticket", "device", "update_type", "details", "created_at", "updated_at"]
    ordering = ["-created_at"]

    @admin.display(description="Device")
    def device_short(self, obj: WalletPassUpdateLog) -> str:
        """Show truncated device ID."""
        if obj.device:
            return f"{obj.device.device_library_id[:12]}..."
        return "-"

    def has_add_permission(self, request: object) -> bool:
        """Prevent manual creation of logs."""
        return False

    def has_change_permission(self, request: object, obj: object = None) -> bool:
        """Prevent modification of logs."""
        return False
