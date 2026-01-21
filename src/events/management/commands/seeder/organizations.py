"""Organization seeding module."""

from datetime import timedelta

from django.utils import timezone

from events.management.commands.seeder.base import BaseSeeder
from events.models import (
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    OrganizationStaff,
    OrganizationToken,
)
from events.models.organization import PermissionMap, PermissionsSchema
from geo.models import City

# Membership tier name options
TIER_NAMES = ["Bronze", "Silver", "Gold", "Platinum", "VIP", "Basic", "Premium"]


class OrganizationSeeder(BaseSeeder):
    """Seeder for organizations and related models."""

    def seed(self) -> None:
        """Seed organizations and related entities."""
        self._create_organizations()
        self._create_membership_tiers()
        self._create_staff()
        self._create_members()
        self._create_tokens()
        self._create_membership_requests()

    def _create_organizations(self) -> None:
        """Create organizations with visibility distribution."""
        self.log(f"Creating {self.config.num_organizations} organizations...")

        # Get cities for location data
        cities = list(City.objects.all()[:50])

        orgs_to_create: list[Organization] = []

        for i, owner in enumerate(self.state.owner_users):
            visibility = self.weighted_choice(self.config.visibility_weights)

            # Map weight key to model value
            visibility_map = {
                "public": Organization.Visibility.PUBLIC,
                "private": Organization.Visibility.PRIVATE,
                "members_only": Organization.Visibility.MEMBERS_ONLY,
                "staff_only": Organization.Visibility.STAFF_ONLY,
            }

            org = Organization(
                name=f"{self.faker.company()} {i}",
                slug=f"org-{i}",
                owner=owner,
                description=self.faker.catch_phrase(),
                visibility=visibility_map[visibility],
                accept_membership_requests=self.random_bool(0.6),
                stripe_account_id=f"acct_test_{i}" if self.random_bool(0.5) else None,
                stripe_charges_enabled=self.random_bool(0.4),
                stripe_details_submitted=self.random_bool(0.4),
                contact_email=self.faker.email() if self.random_bool(0.7) else None,
                contact_email_verified=self.random_bool(0.5),
                city=self.random_choice(cities) if cities else None,
                address=self.faker.address() if self.random_bool(0.8) else None,
            )
            orgs_to_create.append(org)

        self.state.organizations = self.batch_create(Organization, orgs_to_create, desc="Creating organizations")
        self.log(f"  Created {len(self.state.organizations)} organizations")

    def _create_membership_tiers(self) -> None:
        """Create 2-4 membership tiers per organization."""
        self.log("Creating membership tiers...")

        tiers_to_create: list[MembershipTier] = []

        for org in self.state.organizations:
            num_tiers = self.random_int(2, 4)
            tier_names = self.random_sample(TIER_NAMES, num_tiers)

            org_tiers: list[MembershipTier] = []
            for name in tier_names:
                tier = MembershipTier(
                    organization=org,
                    name=name,
                    description=f"{name} membership tier for {org.name}",
                )
                tiers_to_create.append(tier)
                org_tiers.append(tier)

            self.state.membership_tiers[org.id] = org_tiers

        created = self.batch_create(MembershipTier, tiers_to_create, desc="Creating membership tiers")

        # Update state with actual created tiers (with IDs)
        idx = 0
        for org in self.state.organizations:
            num_tiers = len(self.state.membership_tiers[org.id])
            self.state.membership_tiers[org.id] = created[idx : idx + num_tiers]
            idx += num_tiers

        self.log(f"  Created {len(created)} membership tiers")

    def _create_staff(self) -> None:
        """Create staff with randomized permissions."""
        self.log("Creating organization staff...")

        staff_to_create: list[OrganizationStaff] = []
        staff_idx = 0
        min_staff, max_staff = self.config.staff_per_org

        for org in self.state.organizations:
            num_staff = self.random_int(min_staff, max_staff)

            for _ in range(num_staff):
                if staff_idx >= len(self.state.staff_users):
                    break

                user = self.state.staff_users[staff_idx]

                # Generate random permissions
                perms = PermissionMap(
                    view_organization_details=True,
                    create_event=self.random_bool(0.5),
                    create_event_series=self.random_bool(0.3),
                    edit_event_series=self.random_bool(0.4),
                    delete_event_series=self.random_bool(0.2),
                    edit_event=self.random_bool(0.8),
                    delete_event=self.random_bool(0.3),
                    open_event=self.random_bool(0.7),
                    manage_tickets=self.random_bool(0.7),
                    close_event=self.random_bool(0.6),
                    manage_event=self.random_bool(0.7),
                    check_in_attendees=self.random_bool(0.9),
                    invite_to_event=self.random_bool(0.8),
                    edit_organization=self.random_bool(0.2),
                    manage_members=self.random_bool(0.3),
                    manage_potluck=self.random_bool(0.5),
                    create_questionnaire=self.random_bool(0.3),
                    edit_questionnaire=self.random_bool(0.4),
                    delete_questionnaire=self.random_bool(0.2),
                    evaluate_questionnaire=self.random_bool(0.6),
                )

                permissions_schema = PermissionsSchema(default=perms)

                staff_to_create.append(
                    OrganizationStaff(
                        organization=org,
                        user=user,
                        permissions=permissions_schema.model_dump(mode="json"),
                    )
                )
                staff_idx += 1

        self.batch_create(OrganizationStaff, staff_to_create, desc="Creating organization staff")
        self.log(f"  Created {len(staff_to_create)} staff memberships")

    def _create_members(self) -> None:
        """Create members with various statuses and tiers."""
        self.log("Creating organization members...")

        members_to_create: list[OrganizationMember] = []
        min_members, max_members = self.config.members_per_org

        # Keep track of which users are assigned to which orgs
        # to avoid duplicates
        user_org_assignments: set[tuple[str, str]] = set()

        for org in self.state.organizations:
            org_tiers = self.state.membership_tiers.get(org.id, [])
            num_members = self.random_int(min_members, max_members)

            # Select random member users
            available_members = [
                u for u in self.state.member_users if (str(u.id), str(org.id)) not in user_org_assignments
            ]

            if not available_members:
                continue

            member_users = self.random_sample(available_members, min(num_members, len(available_members)))

            org_members: list[OrganizationMember] = []
            for user in member_users:
                user_org_assignments.add((str(user.id), str(org.id)))

                # Status distribution
                status_key = self.weighted_choice(self.config.membership_status_weights)
                status_map = {
                    "active": OrganizationMember.MembershipStatus.ACTIVE,
                    "paused": OrganizationMember.MembershipStatus.PAUSED,
                    "cancelled": OrganizationMember.MembershipStatus.CANCELLED,
                    "banned": OrganizationMember.MembershipStatus.BANNED,
                }

                member = OrganizationMember(
                    organization=org,
                    user=user,
                    status=status_map[status_key],
                    tier=self.random_choice(org_tiers) if org_tiers else None,
                )
                members_to_create.append(member)
                org_members.append(member)

            self.state.org_members[org.id] = org_members

        created = self.batch_create(OrganizationMember, members_to_create, desc="Creating organization members")

        # Update state with actual created members (with IDs)
        idx = 0
        for org in self.state.organizations:
            num_members = len(self.state.org_members.get(org.id, []))
            self.state.org_members[org.id] = created[idx : idx + num_members]
            idx += num_members

        self.log(f"  Created {len(created)} organization members")

    def _create_tokens(self) -> None:
        """Create organization tokens."""
        self.log("Creating organization tokens...")

        tokens_to_create: list[OrganizationToken] = []

        for org in self.state.organizations:
            # 50% of orgs have tokens
            if not self.random_bool(0.5):
                continue

            # Create 1-3 tokens per org
            num_tokens = self.random_int(1, 3)
            org_tiers = self.state.membership_tiers.get(org.id, [])

            for _ in range(num_tokens):
                grants_membership = self.random_bool(0.7)
                grants_staff = self.random_bool(0.2) if not grants_membership else False

                # Determine tier (required if grants_membership)
                tier = None
                if grants_membership and org_tiers:
                    tier = self.random_choice(org_tiers)

                # Skip if grants_membership but no tiers available
                if grants_membership and not tier:
                    continue

                # Expiration: 50% no expiry, 50% expire in 1-30 days
                expires_at = None
                if self.random_bool(0.5):
                    expires_at = timezone.now() + timedelta(days=self.random_int(1, 30))

                tokens_to_create.append(
                    OrganizationToken(
                        organization=org,
                        issuer=org.owner,
                        grants_membership=grants_membership,
                        grants_staff_status=grants_staff,
                        membership_tier=tier,
                        expires_at=expires_at,
                    )
                )

        self.batch_create(OrganizationToken, tokens_to_create, desc="Creating organization tokens")
        self.log(f"  Created {len(tokens_to_create)} organization tokens")

    def _create_membership_requests(self) -> None:
        """Create membership requests for orgs that accept them."""
        self.log("Creating membership requests...")

        requests_to_create: list[OrganizationMembershipRequest] = []

        # Get orgs that accept membership requests
        accepting_orgs = [org for org in self.state.organizations if org.accept_membership_requests]

        # Keep track of existing user-org pairs
        existing_pairs: set[tuple[str, str]] = set()

        # Users who aren't members can request
        for org in accepting_orgs:
            # 5-15 requests per org
            num_requests = self.random_int(5, 15)

            # Get users who aren't already members
            org_member_ids = {str(m.user_id) for m in self.state.org_members.get(org.id, [])}

            available_users = [
                u
                for u in self.state.regular_users
                if str(u.id) not in org_member_ids and (str(u.id), str(org.id)) not in existing_pairs
            ]

            if not available_users:
                continue

            requesting_users = self.random_sample(available_users, min(num_requests, len(available_users)))

            for user in requesting_users:
                existing_pairs.add((str(user.id), str(org.id)))

                # Status distribution: 60% pending, 25% approved, 15% rejected
                status_key = self.weighted_choice(
                    {
                        "pending": 0.6,
                        "approved": 0.25,
                        "rejected": 0.15,
                    }
                )
                status_map = {
                    "pending": OrganizationMembershipRequest.Status.PENDING,
                    "approved": OrganizationMembershipRequest.Status.APPROVED,
                    "rejected": OrganizationMembershipRequest.Status.REJECTED,
                }

                requests_to_create.append(
                    OrganizationMembershipRequest(
                        organization=org,
                        user=user,
                        status=status_map[status_key],
                        message=self.faker.sentence() if self.random_bool(0.7) else "",
                    )
                )

        self.batch_create(
            OrganizationMembershipRequest,
            requests_to_create,
            desc="Creating membership requests",
        )
        self.log(f"  Created {len(requests_to_create)} membership requests")
