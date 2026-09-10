"""Accounts admin package.

Split into submodules to keep each file under the 1000-line limit.
All admin classes are imported here so Django's autodiscovery finds them.
"""

from accounts.admin.billing import UserBillingProfileAdmin as UserBillingProfileAdmin
from accounts.admin.dietary import (
    DietaryPreferenceAdmin as DietaryPreferenceAdmin,
    DietaryRestrictionAdmin as DietaryRestrictionAdmin,
    FoodItemAdmin as FoodItemAdmin,
    UserDietaryPreferenceAdmin as UserDietaryPreferenceAdmin,
)
from accounts.admin.moderation import (
    GlobalBanAdmin as GlobalBanAdmin,
    ImpersonationLogAdmin as ImpersonationLogAdmin,
)
from accounts.admin.referral import (
    ReferralAdmin as ReferralAdmin,
    ReferralCodeAdmin as ReferralCodeAdmin,
    ReferralPayoutAdmin as ReferralPayoutAdmin,
    ReferralPayoutStatementAdmin as ReferralPayoutStatementAdmin,
)
from accounts.admin.tracking import (
    EmailVerificationReminderTrackingAdmin as EmailVerificationReminderTrackingAdmin,
)
from accounts.admin.user import (
    RevelUserAdmin as RevelUserAdmin,
    UserDataExportAdmin as UserDataExportAdmin,
)
